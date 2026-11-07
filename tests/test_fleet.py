"""Tests for fleet control: op locks, desired state, watchdog, system ops."""

from __future__ import annotations

import threading
from pathlib import Path

from vhtk.config import WatchdogSettings
from vhtk.fleet import (
    DesiredState,
    FleetController,
    OpLocks,
    Watchdog,
    WatchdogAction,
    device_phase,
    needs_human,
)


class Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def wd(clock: Clock, **cfg: object) -> Watchdog:
    base: dict[str, object] = {
        "backoff_seconds": [60, 180, 600], "max_restarts_per_hour": 5,
        "hang_timeout": 300, "healthy_after": 600,
    }
    base.update(cfg)
    return Watchdog(WatchdogSettings.model_validate(base), clock=clock)


ABORTED = {"status": "aborted", "task": "farm",
           "reason": "StateTimeout: did not arrive (gave up after 3 recover attempts)"}


def dead(w: Watchdog, state: dict[str, object] = ABORTED) -> WatchdogAction | None:
    return w.decide("farm01", desired="running", busy=False, alive=False, state=state)


# -- needs_human -----------------------------------------------------------------


def test_needs_human_classification() -> None:
    assert not needs_human(ABORTED)
    assert not needs_human({"status": "aborted", "task": "farm",
                            "reason": "requested by state 'TAP_SPOT'"})  # mất mạng
    assert not needs_human({"status": "crashed", "reason": "device lost"})
    assert needs_human({"status": "aborted", "task": "farm",
                        "reason": "requested by state 'VERIFY_HUD'"})  # thiếu config/template
    assert needs_human({"status": "aborted", "task": "login",
                        "reason": "requested by state 'CHECK_SERVER'"})  # sai server
    assert needs_human({"status": "aborted", "task": "sell",
                        "reason": "SafetyLimitExceeded: 200 irreversible actions"})
    assert needs_human({"status": "stopped", "reason": "bag_full_loop"})
    assert needs_human({"status": "crashed", "reason": "invalid config in x.yaml"})


# -- watchdog ----------------------------------------------------------------------


def test_watchdog_backoff_then_escalates_to_reboot() -> None:
    clock = Clock()
    w = wd(clock)
    assert dead(w) is None  # vừa phát hiện: ghi nhận + hẹn 60s
    v = w.view("farm01")
    assert v["failed"] and v["retry_in"] == 60 and v["consecutive"] == 1
    clock.t += 59
    assert dead(w) is None
    clock.t += 1
    assert dead(w) == WatchdogAction("farm01", "start")  # lần 1: chỉ chạy lại worker
    # worker mới lại chết ngay
    assert w.decide("farm01", desired="running", busy=False, alive=True, state={}) is None
    assert dead(w) is None
    assert w.view("farm01")["retry_in"] == 180  # lần 2: giãn 180s
    clock.t += 180
    assert dead(w) == WatchdogAction("farm01", "reboot")  # từ lần 2: tắt/bật cả máy


def test_watchdog_gives_up_after_hourly_limit() -> None:
    clock = Clock()
    w = wd(clock, max_restarts_per_hour=2, backoff_seconds=[1])
    for _ in range(2):
        dead(w)
        clock.t += 1
        assert dead(w) is not None
        w.decide("farm01", desired="running", busy=False, alive=True, state={})
    assert dead(w) is None  # lần hỏng thứ 3 trong giờ
    v = w.view("farm01")
    assert v["gave_up"] and v["retry_in"] is None
    clock.t += 10_000
    assert dead(w) is None  # đã bỏ cuộc thì không tự chạy lại nữa


def test_watchdog_never_restarts_human_failures_or_stopped_devices() -> None:
    clock = Clock()
    w = wd(clock)
    human = {"status": "aborted", "task": "farm", "reason": "requested by state 'VERIFY_HUD'"}
    assert dead(w, human) is None
    clock.t += 10_000
    assert dead(w, human) is None
    assert w.view("farm01")["gave_up"]
    # người đã bấm Dừng (desired stopped) hoặc đang có thao tác -> không động vào
    w2 = wd(clock)
    assert w2.decide("x", desired="stopped", busy=False, alive=False, state=ABORTED) is None
    assert w2.decide("x", desired="running", busy=True, alive=False, state=ABORTED) is None
    assert not w2.view("x")["failed"]
    # dừng bằng kill-switch = chủ ý
    assert w2.decide("x", desired="running", busy=False, alive=False,
                     state={"status": "killed"}) is None


def test_watchdog_detects_hung_worker_and_restarts_after_kill() -> None:
    clock = Clock()
    w = wd(clock)
    state = {"status": "running", "heartbeat_at": clock.t}
    assert w.decide("farm01", desired="running", busy=False, alive=True, state=state) is None
    clock.t += 301  # sống nhưng không có nhịp tim > hang_timeout
    action = w.decide("farm01", desired="running", busy=False, alive=True, state=state)
    assert action == WatchdogAction("farm01", "kill_hung")
    # sau khi watchdog tự dừng, file trạng thái ghi "killed" -> vẫn phải chạy lại
    assert dead(w, {"status": "killed"}) is None
    clock.t += 60
    assert dead(w, {"status": "killed"}) == WatchdogAction("farm01", "start")


def test_watchdog_healthy_run_resets_consecutive() -> None:
    clock = Clock()
    w = wd(clock, backoff_seconds=[1])
    dead(w)
    clock.t += 1
    dead(w)
    w.decide("farm01", desired="running", busy=False, alive=True, state={})
    clock.t += 601
    w.decide("farm01", desired="running", busy=False, alive=True, state={})
    assert w.view("farm01")["consecutive"] == 0
    dead(w)
    clock.t += 1
    assert dead(w) == WatchdogAction("farm01", "start")  # không leo thang reboot


class FakeSup:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.alive: set[str] = set()
        self.lock = threading.Lock()

    def start(self, stem: str) -> int:
        with self.lock:
            self.calls.append(("start", stem))
            self.alive.add(stem)
        return 1

    def stop(self, stem: str) -> None:
        with self.lock:
            self.calls.append(("stop", stem))
            self.alive.discard(stem)

    def is_alive(self, stem: str) -> bool:
        return stem in self.alive


def test_watchdog_execute_maps_actions_to_supervisor() -> None:
    w = wd(Clock())
    sup = FakeSup()
    w.execute(WatchdogAction("a", "start"), sup)
    w.execute(WatchdogAction("b", "reboot"), sup)
    w.execute(WatchdogAction("c", "kill_hung"), sup)
    assert sup.calls == [("start", "a"), ("stop", "b"), ("start", "b"), ("stop", "c")]


# -- locks / desired ---------------------------------------------------------------


def test_op_locks_one_op_per_device() -> None:
    locks = OpLocks()
    assert locks.try_begin("a", "start") is None
    assert locks.try_begin("a", "stop") == "start"
    assert locks.try_begin("b", "stop") is None
    locks.end("a")
    assert locks.current("a") is None and locks.try_begin("a", "stop") is None


def test_desired_state_persists(tmp_path: Path) -> None:
    p = tmp_path / "w" / "desired.json"
    d = DesiredState(p)
    assert d.get("a") == "stopped"
    d.set("a", "running")
    assert DesiredState(p).get("a") == "running"
    p.write_text("{rác", encoding="utf-8")
    assert DesiredState(p).get("a") == "stopped"  # file hỏng = chưa muốn chạy gì


# -- system ops --------------------------------------------------------------------


def fleet(tmp_path: Path, sup: FakeSup, limit: int = 5) -> FleetController:
    return FleetController(OpLocks(), DesiredState(tmp_path / "d.json"), wd(Clock()),
                           lambda: sup, lambda: limit)


def test_start_all_respects_concurrency_and_skips_running(tmp_path: Path) -> None:
    sup = FakeSup()
    sup.alive.add("f1")
    f = fleet(tmp_path, sup, limit=2)
    assert f.begin("start_all", 3)
    f.run("start_all", ["f1", "f2", "f3"], ["f1", "f2", "f3", "main"])
    assert sup.calls == [("start", "f2")]  # f1 đang chạy, f3 vượt giới hạn 2 máy
    assert any("f3" in e for e in f.status.errors)
    assert f.desired.get("f1") == "running" and f.desired.get("f2") == "running"
    assert f.desired.get("f3") == "stopped"
    assert not f.busy() and f.status.done == 3


def test_stop_all_marks_stopped_first_and_stops_everyone(tmp_path: Path) -> None:
    sup = FakeSup()
    sup.alive |= {"f1", "main"}
    f = fleet(tmp_path, sup)
    f.desired.set("f1", "running")
    assert f.begin("stop_all", 3)
    f.run("stop_all", ["f1"], ["f1", "f2", "main"])
    assert sorted(c for c in sup.calls) == [("stop", "f1"), ("stop", "f2"), ("stop", "main")]
    assert all(f.desired.get(s) == "stopped" for s in ("f1", "f2", "main"))


def test_restart_all_and_single_flight(tmp_path: Path) -> None:
    sup = FakeSup()
    sup.alive.add("f1")
    f = fleet(tmp_path, sup)
    assert f.begin("restart_all", 2)
    assert not f.begin("stop_all", 1)  # đang có thao tác toàn hệ thống
    f.run("restart_all", ["f1"], ["f1"])
    assert sup.calls == [("stop", "f1"), ("start", "f1")]
    assert f.begin("stop_all", 1)  # xong rồi thì nhận lệnh mới


def test_busy_device_is_skipped_by_system_op(tmp_path: Path) -> None:
    sup = FakeSup()
    f = fleet(tmp_path, sup)
    f.locks.try_begin("f1", "start")  # đang bật dở bằng nút riêng
    f.begin("start_all", 1)
    f.run("start_all", ["f1"], ["f1"])
    assert sup.calls == []
    assert any("f1" in e for e in f.status.errors)


# -- device_phase ------------------------------------------------------------------


def test_device_phase_precedence() -> None:
    ok: dict[str, object] = {"failed": False, "gave_up": False}
    kw: dict[str, object] = dict(op=None, running=False, instance=False, health=ok,
                                 state={}, desired="stopped")
    assert device_phase(**{**kw, "op": "start"}) == "busy"  # type: ignore[arg-type]
    assert device_phase(**{**kw, "running": True}) == "running"  # type: ignore[arg-type]
    assert device_phase(**{**kw, "instance": True}) == "device_on"  # type: ignore[arg-type]
    assert device_phase(**kw) == "off"  # type: ignore[arg-type]
    assert device_phase(**{**kw, "health": {"failed": True}}) == "failed"  # type: ignore[arg-type]
    assert device_phase(**{**kw, "health": {"gave_up": True}}) == "needs_human"  # type: ignore[arg-type]
    # máy bật + worker vừa chết lúc đang muốn chạy -> đỏ, KHÔNG phải "máy bật"
    assert device_phase(**{**kw, "instance": True, "desired": "running",  # type: ignore[arg-type]
                           "state": ABORTED}) == "failed"
