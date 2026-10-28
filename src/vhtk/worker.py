"""One account's full lifecycle on one device, as an independent OS process.

This is the ``AccountWorker`` of PLAN.md 5b: it knows NOTHING about other
instances — the orchestrator (W2) just spawns N of these, one process per
LDPlayer instance. Loop:

    FARM -> signal?
        HP_LOW         -> RETREAT -> [SELL every N retreats] -> FARM
        BAG_FULL       -> RETREAT -> SELL (always)           -> FARM
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
from vhtk.tasks.farm import FarmSignal, FarmTask
from vhtk.tasks.login import LoginTask
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
        retreats = 0
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
            if signal is FarmSignal.GOLD_THRESHOLD:
                # W3 mới giao dịch — giờ chỉ ghi cờ cho orchestrator + báo người
                self.status.update(gold_request=True)
                self.ctx.notifier.send_text(
                    f"[{self.stem}] gold threshold reached — waiting for W3 transfer"
                )
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
                retreats += 1
                if (
                    signal is FarmSignal.BAG_FULL
                    or retreats % self.ctx.settings.sell.every_n_retreats == 0
                ):
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
            else:
                bag_full_streak = 0
            if self.max_cycles and cycles >= self.max_cycles:
                self.status.update(status="stopped", reason="max_cycles")
                return 0

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
    account = load_account(account_path)
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
        return worker.run()
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
