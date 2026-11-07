"""Tests for the AccountWorker loop (pure logic, fake tasks, no device)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from vhtk.config import AccountConfig, load_settings
from vhtk.core.fsm import TaskContext, TaskResult, TaskStatus
from vhtk.tasks.farm import FarmSignal
from vhtk.tasks.money import Money
from vhtk.worker import AccountWorker, StatusWriter, WorkerTasks, state_file

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


def ok(state: str = "MONITOR") -> TaskResult:
    return TaskResult(TaskStatus.SUCCESS, state, "", 0)


def result(status: TaskStatus, reason: str = "") -> TaskResult:
    return TaskResult(status, "X", reason, 0)


class FakeTask:
    def __init__(self, results: Iterator[TaskResult]) -> None:
        self._results = results

    def run(self) -> TaskResult:
        return next(self._results)


class FakeFarm(FakeTask):
    def __init__(self, results: Iterator[TaskResult], signals: Iterator[FarmSignal | None]):
        super().__init__(results)
        self._signals = signals
        self.signal: FarmSignal | None = None

    def run(self) -> TaskResult:
        self.signal = next(self._signals)
        return super().run()


class FakeSell(FakeTask):
    class _Report:
        sold = 0
        would_sell = 0
        skipped_numbered = 0
        skipped_quality = 0
        money = Money()

    report = _Report()


class RecordingNotifier:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def send_text(self, text: str) -> None:
        self.texts.append(text)

    def send_photo(self, image: Any, caption: str) -> None:  # pragma: no cover
        pass

    def wait_confirm(self, prompt: str, image: Any, timeout: float) -> bool:  # pragma: no cover
        return False


def make_worker(
    tmp_path: Path,
    *,
    farm_results: list[TaskResult],
    farm_signals: list[FarmSignal | None],
    retreat_results: list[TaskResult] = (),
    sell_results: list[TaskResult] = (),
    login_results: list[TaskResult] = (),
    max_cycles: int = 0,
) -> tuple[AccountWorker, RecordingNotifier, Path]:
    settings = load_settings(SETTINGS_PATH)
    notifier = RecordingNotifier()
    ctx = TaskContext(
        device=None,  # loop không đụng thiết bị — task giả hết
        vision=None,
        safety=None,
        recovery=None,
        notifier=notifier,
        settings=settings,
        account=None,
        log_dir=tmp_path,
    )
    farm = FakeFarm(iter(farm_results), iter(farm_signals))
    tasks = WorkerTasks(
        farm=lambda _ctx: farm,
        retreat=lambda _ctx: FakeTask(iter(retreat_results)),
        sell=lambda _ctx: FakeSell(iter(sell_results)),
        login=lambda _ctx: FakeTask(iter(login_results)),
    )
    sf = state_file(tmp_path / "workers", "dev")
    worker = AccountWorker(
        ctx=ctx,
        stem="dev",
        status=StatusWriter(sf, "dev"),
        tasks=tasks,
        max_cycles=max_cycles,
    )
    return worker, notifier, sf


def read_state(sf: Path) -> dict[str, Any]:
    return json.loads(sf.read_text(encoding="utf-8"))


def test_plain_cycles_until_max(tmp_path: Path) -> None:
    worker, _, sf = make_worker(
        tmp_path,
        farm_results=[ok(), ok(), ok()],
        farm_signals=[None, None, None],
        max_cycles=3,
    )
    assert worker.run() == 0
    st = read_state(sf)
    assert st["status"] == "stopped" and st["cycle"] == 3


def test_hp_low_retreats_but_never_sells(tmp_path: Path) -> None:
    # HP_LOW chỉ về nghỉ rồi farm lại — KHÔNG bán tiện thể. sell_results
    # rỗng: nếu worker gọi sell, FakeSell.run() ném StopIteration -> test đổ
    worker, _, _ = make_worker(
        tmp_path,
        farm_results=[ok(), ok()],
        farm_signals=[FarmSignal.HP_LOW, FarmSignal.HP_LOW],
        retreat_results=[ok(), ok()],
        sell_results=[],
        max_cycles=2,
    )
    assert worker.run() == 0


def test_bag_full_always_sells_and_loop_guard_stops(tmp_path: Path) -> None:
    worker, notifier, sf = make_worker(
        tmp_path,
        farm_results=[ok(), ok(), ok()],
        farm_signals=[FarmSignal.BAG_FULL] * 3,
        retreat_results=[ok()] * 3,
        sell_results=[ok()] * 3,
    )
    assert worker.run() == 1  # bán 2 lần túi vẫn đầy -> dừng báo người
    assert read_state(sf)["reason"] == "bag_full_loop"
    assert any("bag still full" in t for t in notifier.texts)


def make_account(threshold: int, keep: int = 0) -> AccountConfig:
    return AccountConfig.model_validate(
        {
            "name": "dev",
            "role": "farm",
            "ldplayer": {"index": 0, "adb_serial": "emulator-5554"},
            "login": {"server": "S9", "username": "", "password": ""},
            "farm": {
                "map": "Bắc Tuyền Châu",
                "gold_transfer_threshold": threshold,
                "gold_keep": keep,
            },
        }
    )


def test_gold_threshold_flags_state_and_notifies(tmp_path: Path) -> None:
    """Tiền đọc lúc bán (BAG_FULL -> sell) đạt ngưỡng -> cờ gold_request + báo người."""
    worker, notifier, sf = make_worker(
        tmp_path,
        farm_results=[ok()],
        farm_signals=[FarmSignal.BAG_FULL],
        retreat_results=[ok()],
        sell_results=[ok()],
        max_cycles=1,
    )
    worker.ctx.account = make_account(threshold=500)
    worker.ctx.settings = worker.ctx.settings.model_copy(
        update={"transfer": worker.ctx.settings.transfer.model_copy(update={"currency": "bac"})}
    )
    FakeSell.report.money = Money(vang=0, bac=750, dong=3)
    try:
        assert worker.run() == 0
    finally:
        FakeSell.report.money = Money()
    st = read_state(sf)
    assert st["gold_request"] is True
    assert st["money"] == {"vang": 0, "bac": 750, "dong": 3}
    assert any("gold threshold" in t for t in notifier.texts)


def test_gold_threshold_ignores_unreadable_money(tmp_path: Path) -> None:
    worker, notifier, sf = make_worker(
        tmp_path,
        farm_results=[ok()],
        farm_signals=[FarmSignal.BAG_FULL],
        retreat_results=[ok()],
        sell_results=[ok()],
        max_cycles=1,
    )
    worker.ctx.account = make_account(threshold=1)
    FakeSell.report.money = Money()  # OCR không đọc được -> không bao giờ coi là đạt ngưỡng
    assert worker.run() == 0
    assert "gold_request" not in read_state(sf)
    assert not any("gold threshold" in t for t in notifier.texts)


class FakeTransfer(FakeTask):
    class _Report:
        amount = 0
        verified = False
        would_transfer = False

    def __init__(self, results: Iterator[TaskResult], verified: bool) -> None:
        super().__init__(results)
        self.report = self._Report()
        self.report.verified = verified
        self.report.amount = 1000


def test_transfer_signal_runs_farm_side_and_clears_request(tmp_path: Path) -> None:
    worker, _, sf = make_worker(
        tmp_path,
        farm_results=[ok(), ok()],
        farm_signals=[FarmSignal.TRANSFER, None],
        max_cycles=2,
    )
    worker.ctx.account = make_account(threshold=1)
    workers_dir = tmp_path / "workers"
    worker.ctx.settings = worker.ctx.settings.model_copy(
        update={
            "orchestrator": worker.ctx.settings.orchestrator.model_copy(
                update={"workers_dir": workers_dir}
            )
        }
    )
    request = workers_dir / "dev.transfer.json"
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text("{}", encoding="utf-8")
    worker.tasks = WorkerTasks(
        farm=worker.tasks.farm,
        retreat=worker.tasks.retreat,
        sell=worker.tasks.sell,
        login=worker.tasks.login,
        transfer_farm=lambda _ctx: FakeTransfer(iter([ok("VERIFY_AFTER")]), verified=True),
    )
    assert worker.run() == 0
    st = read_state(sf)
    assert st["transfer"]["verified"] is True
    assert st["gold_request"] is False  # chuyển xong -> hạ cờ
    assert not request.exists()  # file yêu cầu bị xoá dù kết quả ra sao
    assert st["cycle"] == 2  # worker quay lại farm sau giao dịch


def test_transfer_failure_keeps_flag_and_continues_farming(tmp_path: Path) -> None:
    worker, notifier, sf = make_worker(
        tmp_path,
        farm_results=[ok(), ok()],
        farm_signals=[FarmSignal.TRANSFER, None],
        max_cycles=2,
    )
    worker.ctx.account = make_account(threshold=1)
    worker.tasks = WorkerTasks(
        farm=worker.tasks.farm,
        retreat=worker.tasks.retreat,
        sell=worker.tasks.sell,
        login=worker.tasks.login,
        transfer_farm=lambda _ctx: FakeTransfer(
            iter([result(TaskStatus.ABORTED, "partner timeout")]), verified=False
        ),
    )
    assert worker.run() == 0  # giao dịch hỏng KHÔNG giết worker
    st = read_state(sf)
    assert st["gold_request"] is True
    assert st["cycle"] == 2
    assert any("transfer failed" in t for t in notifier.texts)


def test_main_role_logs_in_receives_once_and_exits(tmp_path: Path) -> None:
    worker, _, sf = make_worker(
        tmp_path,
        farm_results=[],
        farm_signals=[],
        login_results=[ok("VERIFY_INGAME")],
    )
    worker.tasks = WorkerTasks(
        farm=worker.tasks.farm,
        retreat=worker.tasks.retreat,
        sell=worker.tasks.sell,
        login=worker.tasks.login,
        transfer_main=lambda _ctx: FakeTransfer(iter([ok("VERIFY_AFTER")]), verified=True),
    )
    assert worker.run_main() == 0
    st = read_state(sf)
    assert st["status"] == "done" and st["transfer"]["verified"] is True


def test_relogin_runs_login_then_retries(tmp_path: Path) -> None:
    worker, _, _ = make_worker(
        tmp_path,
        farm_results=[result(TaskStatus.RELOGIN_REQUIRED), ok()],
        farm_signals=[None, None],
        login_results=[ok("DONE")],
        max_cycles=1,
    )
    assert worker.run() == 0


def test_killed_stops_cleanly_with_exit_zero(tmp_path: Path) -> None:
    worker, _, sf = make_worker(
        tmp_path,
        farm_results=[result(TaskStatus.KILLED, "STOP file")],
        farm_signals=[None],
    )
    assert worker.run() == 0
    assert read_state(sf)["status"] == "killed"


def test_abort_returns_one_and_records_reason(tmp_path: Path) -> None:
    worker, _, sf = make_worker(
        tmp_path,
        farm_results=[result(TaskStatus.ABORTED, "boom")],
        farm_signals=[None],
    )
    assert worker.run() == 1
    st = read_state(sf)
    assert st["status"] == "aborted" and st["reason"] == "boom"


def test_status_writer_heartbeat_is_throttled(tmp_path: Path) -> None:
    import json as _json

    from vhtk.worker import StatusWriter

    t = {"now": 100.0}
    path = tmp_path / "x.state.json"
    w = StatusWriter(path, "x", clock=lambda: t["now"])
    w.beat()
    assert _json.loads(path.read_text())["heartbeat_at"] == 100.0
    t["now"] = 110.0
    w.beat()  # chưa đủ 20s -> không ghi lại
    assert _json.loads(path.read_text())["heartbeat_at"] == 100.0
    t["now"] = 121.0
    w.beat()
    assert _json.loads(path.read_text())["heartbeat_at"] == 121.0
