"""One account's full lifecycle on one device, as an independent OS process.

This is the ``AccountWorker`` of PLAN.md 5b: it knows NOTHING about other
instances — the orchestrator (W2) just spawns N of these, one process per
LDPlayer instance. Loop:

    FARM -> signal?
        HP_LOW         -> RETREAT                 -> FARM
        BAG_FULL       -> RETREAT -> SELL         -> FARM
    (SELL chỉ chạy khi túi đầy — không bán "tiện thể" mỗi lần về nghỉ,
    để sau này bỏ bước nghỉ cũng không ảnh hưởng luồng bán.)
        GOLD_THRESHOLD -> flag it in the state file + notify (W3 trades it)
    RELOGIN_REQUIRED from any task -> LOGIN -> retry the task
    ABORTED -> exit 1 (evidence already saved by the task)
    KILLED  -> exit 0 (kill-switch: global STOP or per-account STOP.<stem>)

While running, the worker reports task/state into ``logs/workers/<stem>.state.json``
(atomic writes) and registers its PID in the worker registry — that file pair
is the ONLY thing GUI/orchestrator read; they never reach into this process.

Run:
    python -m vhtk.worker --account config/accounts/farm01.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

from vhtk.config import Settings, load_account, load_settings
from vhtk.core.device import Device
from vhtk.core.fsm import TaskContext, TaskResult, TaskStatus
from vhtk.core.recovery import Recovery
from vhtk.core.safety import Safety
from vhtk.core.vision import TemplateStore, Vision
from vhtk.logging_setup import setup_logging
from vhtk.notify import NullNotifier
from vhtk.registry import WorkerRegistry
from vhtk.tasks.farm import FarmSignal, FarmTask, transfer_request_file
from vhtk.tasks.gold_transfer import TransferFarmTask, TransferMainTask
from vhtk.tasks.login import LoginTask
from vhtk.tasks.money import reached_threshold
from vhtk.tasks.retreat import RetreatTask
from vhtk.tasks.sell import SellTask

log = structlog.get_logger(__name__)

# Túi vẫn đầy sau N lần bán liên tiếp (dry_run hoặc blacklist giữ hết) thì
# dừng báo người — bán tiếp chỉ lặp vô ích
BAG_FULL_STREAK_LIMIT = 2


class _RunnableTask(Protocol):
    def run(self) -> TaskResult: ...


TaskFactory = Callable[[TaskContext], _RunnableTask]


@dataclass(frozen=True)
class WorkerTasks:
    """Task factories, injectable so the loop is testable without a device."""

    farm: Callable[[TaskContext], FarmTask] = FarmTask
    retreat: TaskFactory = RetreatTask
    sell: Callable[[TaskContext], SellTask] = SellTask
    login: TaskFactory = LoginTask
    transfer_farm: Callable[[TaskContext], TransferFarmTask] = TransferFarmTask
    transfer_main: Callable[[TaskContext], TransferMainTask] = TransferMainTask


class StatusWriter:
    """Atomically maintain this worker's state file for GUI/orchestrator."""

    def __init__(self, path: Path, stem: str) -> None:
        self._path = path
        self._stem = stem
        self._data: dict[str, Any] = {"stem": stem, "pid": os.getpid()}

    def update(self, **fields: Any) -> None:
        """Merge fields and rewrite the state file; never raises."""
        self._data.update(fields)
        self._data["updated_at"] = time.time()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:  # báo cáo trạng thái không bao giờ được giết worker
            log.warning("cannot write state file", path=str(self._path), error=str(exc))


def state_file(workers_dir: Path, stem: str) -> Path:
    """Path of one worker's state file (shared convention with the GUI)."""
    return workers_dir / f"{stem}.state.json"


@dataclass
class AccountWorker:
    """The single-account loop; pure orchestration of existing tasks."""

    ctx: TaskContext
    stem: str
    status: StatusWriter
    tasks: WorkerTasks = field(default_factory=WorkerTasks)
    max_cycles: int = 0
    max_relogins: int = 2

    def _run_with_relogin(self, task: _RunnableTask) -> TaskResult:
        """Run one task; on RELOGIN_REQUIRED, log in and retry (bounded).

        Keeps the SAME task instance across retries so attributes like
        ``FarmTask.signal`` stay readable afterwards.
        """
        result = task.run()
        relogins = 0
        while result.status is TaskStatus.RELOGIN_REQUIRED and relogins < self.max_relogins:
            relogins += 1
            log.info("thrown back to login, re-logging in", stem=self.stem)
            login = self.tasks.login(self.ctx).run()
            if login.status is not TaskStatus.SUCCESS:
                return login
            result = task.run()
        return result

    def run(self) -> int:
        """Run the lifecycle until ABORT (1) or kill-switch/cycle limit (0)."""
        cycles = 0
        bag_full_streak = 0
        self.status.update(status="running", task="", state="", signal="", cycle=0)
        # chạy FARM trước, không login trước: FarmTask tự xử lý màn hình bất
        # kỳ; chưa đăng nhập thì recovery báo RELOGIN và _run_with_relogin lo
        while True:
            cycles += 1
            self.status.update(cycle=cycles)
            log.info("farm cycle", stem=self.stem, cycle=cycles)
            farm = self.tasks.farm(self.ctx)
            result = self._run_with_relogin(farm)
            if (rc := self._ended(result, "farm")) is not None:
                return rc
            signal = farm.signal
            self.status.update(signal=signal.value if signal else "")
            if signal is FarmSignal.TRANSFER and (rc := self._run_transfer()) is not None:
                return rc
            if signal in (FarmSignal.HP_LOW, FarmSignal.BAG_FULL):
                if signal is FarmSignal.BAG_FULL:
                    bag_full_streak += 1
                    if bag_full_streak > BAG_FULL_STREAK_LIMIT:
                        log.error(
                            "bag still full after selling — needs a human",
                            stem=self.stem,
                        )
                        self.ctx.notifier.send_text(
                            f"[{self.stem}] bag still full after selling twice; stopped"
                        )
                        self.status.update(status="stopped", reason="bag_full_loop")
                        return 1
                else:
                    bag_full_streak = 0
                retreat = self._run_with_relogin(self.tasks.retreat(self.ctx))
                if (rc := self._ended(retreat, "retreat")) is not None:
                    return rc
                # chỉ túi đầy mới đi bán — về nghỉ vì HP thấp thì farm lại ngay
                if signal is FarmSignal.BAG_FULL:
                    sell = self.tasks.sell(self.ctx)
                    sres = self._run_with_relogin(sell)
                    if sres.status is TaskStatus.KILLED:
                        self.status.update(status="killed")
                        return 0
                    r = sell.report
                    log.info(
                        "sell finished",
                        stem=self.stem,
                        status=sres.status.value,
                        sold=r.sold,
                        would_sell=r.would_sell,
                    )
                    # bán lỗi không chặn vòng farm — thử lại ở chu kỳ sau
                    self._note_money(r.money)
            else:
                bag_full_streak = 0
            if self.max_cycles and cycles >= self.max_cycles:
                self.status.update(status="stopped", reason="max_cycles")
                return 0

    def _note_money(self, money: Any) -> None:
        """Record the money read while the bag was open; raise the gold flag."""
        if self.ctx.account is None:
            return
        cfg = self.ctx.settings.transfer
        threshold = self.ctx.account.farm.gold_transfer_threshold
        self.status.update(money=money.as_dict())
        if reached_threshold(money, cfg.currency, threshold):
            log.info(
                "gold threshold reached",
                stem=self.stem,
                currency=cfg.currency,
                balance=money.get(cfg.currency),
                threshold=threshold,
            )
            self.status.update(gold_request=True)
            self.ctx.notifier.send_text(
                f"[{self.stem}] gold threshold reached: {money.get(cfg.currency)} "
                f"{cfg.currency} >= {threshold}"
            )

    def _run_transfer(self) -> int | None:
        """Farm side of one trade session; never fatal except kill-switch.

        Thất bại (main không tới, OCR lệch...) chỉ log + báo người, worker
        quay lại farm; orchestrator quyết định thử lại sau. File yêu cầu bị
        xoá dù kết quả ra sao để MONITOR không thấy lại lệnh cũ.
        """
        request = transfer_request_file(self.ctx.settings.orchestrator.workers_dir, self.stem)
        try:
            task = self.tasks.transfer_farm(self.ctx)
            result = self._run_with_relogin(task)
            if result.status is TaskStatus.KILLED:
                self.status.update(status="killed", task="transfer_farm")
                return 0
            rep = task.report
            log.info(
                "transfer finished",
                stem=self.stem,
                status=result.status.value,
                amount=rep.amount,
                verified=rep.verified,
                would_transfer=rep.would_transfer,
            )
            self.status.update(
                transfer={
                    "status": result.status.value,
                    "amount": rep.amount,
                    "verified": rep.verified,
                    "would_transfer": rep.would_transfer,
                },
                # chuyển xong (đã xác minh) thì hạ cờ; còn lại giữ cờ cho lần sau
                gold_request=not rep.verified,
            )
            if not rep.verified and not rep.would_transfer:
                self.ctx.notifier.send_text(
                    f"[{self.stem}] transfer failed ({result.status.value}: {result.reason})"
                )
            return None
        finally:
            request.unlink(missing_ok=True)

    def run_main(self) -> int:
        """Main-account lifecycle: log in, receive ONE trade, finish."""
        self.status.update(status="running", task="", state="", signal="", cycle=1)
        login = self.tasks.login(self.ctx).run()
        if (rc := self._ended(login, "login")) is not None:
            return rc
        task = self.tasks.transfer_main(self.ctx)
        result = self._run_with_relogin(task)
        if result.status is TaskStatus.KILLED:
            self.status.update(status="killed", task="transfer_main")
            return 0
        rep = task.report
        self.status.update(
            status="done",
            task="transfer_main",
            transfer={
                "status": result.status.value,
                "amount": rep.amount,
                "verified": rep.verified,
            },
        )
        log.info("main transfer finished", status=result.status.value, verified=rep.verified)
        return 0 if result.status is TaskStatus.SUCCESS else 1

    def _ended(self, result: TaskResult, task_name: str) -> int | None:
        """Exit code if the loop must stop after this result, else None."""
        if result.status is TaskStatus.KILLED:
            log.info("kill-switch — stopped cleanly", stem=self.stem, task=task_name)
            self.status.update(status="killed", task=task_name)
            return 0
        if result.status is not TaskStatus.SUCCESS:
            log.error(
                "task ended abnormally",
                stem=self.stem,
                task=task_name,
                status=result.status.value,
                reason=result.reason,
            )
            self.status.update(status="aborted", task=task_name, reason=result.reason)
            return 1
        return None


def build_context(
    settings: Settings, account_path: Path, *, on_state: Callable[[str, str], None] | None = None
) -> TaskContext:
    """Wire device/vision/safety/recovery for one account, with the
    per-account kill-switch (``STOP.<stem>``) armed."""
    stem = account_path.stem
    account = load_account(account_path, settings.farm_defaults)
    store = TemplateStore(settings.templates.root)
    device = Device(
        account.ldplayer.adb_serial, settings.safety, settings.ldplayer.resolution, stem=stem
    )
    vision = Vision(store, device.screenshot, settings.vision, settings.templates.match_threshold)
    notifier = NullNotifier()
    safety = Safety(settings.safety, notifier, settings.logging.dir / "dry_run", stem=stem)
    recovery = Recovery(
        device,
        vision,
        notifier,
        settings.logging.dir / "unknown",
        app_in_foreground=lambda: device.app_in_foreground(settings.game.package_name),
    )
    return TaskContext(
        device=device,
        vision=vision,
        safety=safety,
        recovery=recovery,
        notifier=notifier,
        settings=settings,
        account=account,
        log_dir=settings.logging.dir,
        on_state=on_state,
    )


def run_worker(settings_path: Path, account_path: Path, max_cycles: int = 0) -> int:
    """Entry point shared by ``python -m vhtk.worker`` and tools/run_farm.py."""
    settings = load_settings(settings_path)
    setup_logging(settings.logging.dir, settings.logging.level)
    stem = account_path.stem
    workers_dir = settings.orchestrator.workers_dir
    status = StatusWriter(state_file(workers_dir, stem), stem)
    registry = WorkerRegistry(workers_dir)
    registry.register(stem, os.getpid(), account_path)
    ctx = build_context(
        settings,
        account_path,
        on_state=lambda task, st: status.update(task=task, state=st),
    )
    worker = AccountWorker(ctx=ctx, stem=stem, status=status, max_cycles=max_cycles)
    try:
        assert ctx.account is not None
        # acc main (role: main) chỉ sống một chuyến giao dịch rồi thoát
        return worker.run_main() if ctx.account.role == "main" else worker.run()
    except Exception as exc:
        log.error("worker crashed", stem=stem, error=str(exc))
        status.update(status="crashed", reason=str(exc))
        raise
    finally:
        registry.unregister(stem)
        ctx.device.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one account's lifecycle on one device")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--account", required=True, help="account YAML, e.g. accounts/farm01.yaml")
    parser.add_argument(
        "--max-cycles", type=int, default=0,
        help="stop after N completed farm runs (0 = run until ABORT/kill-switch)",
    )
    args = parser.parse_args(argv)
    return run_worker(Path(args.settings), Path(args.account), args.max_cycles)


if __name__ == "__main__":
    sys.exit(main())
