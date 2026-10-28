"""Tests for the AccountWorker loop (pure logic, fake tasks, no device)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from vhtk.config import load_settings
from vhtk.core.fsm import TaskContext, TaskResult, TaskStatus
from vhtk.tasks.farm import FarmSignal
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
        skipped_unknown = 0
        skipped_quality = 0

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


def test_hp_low_triggers_retreat_and_periodic_sell(tmp_path: Path) -> None:
    # every_n_retreats của settings thật có thể > 1 — ép 2 lần HP_LOW với
    # every_n_retreats=2 thì lần rút thứ 2 phải bán
    worker, _, _ = make_worker(
        tmp_path,
        farm_results=[ok(), ok()],
        farm_signals=[FarmSignal.HP_LOW, FarmSignal.HP_LOW],
        retreat_results=[ok(), ok()],
        sell_results=[ok()],
        max_cycles=2,
    )
    worker.ctx.settings = worker.ctx.settings.model_copy(
        update={"sell": worker.ctx.settings.sell.model_copy(update={"every_n_retreats": 2})}
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


def test_gold_threshold_flags_state_and_notifies(tmp_path: Path) -> None:
    worker, notifier, sf = make_worker(
        tmp_path,
        farm_results=[ok()],
        farm_signals=[FarmSignal.GOLD_THRESHOLD],
        max_cycles=1,
    )
    assert worker.run() == 0
    assert read_state(sf)["gold_request"] is True
    assert any("gold threshold" in t for t in notifier.texts)


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
