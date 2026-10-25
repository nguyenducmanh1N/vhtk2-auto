"""Tests for the task state machine: transitions, recovery routing, kill-switch.

Everything runs with fakes — no device, no templates, no network.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import numpy as np
import pytest

from vhtk.config import Settings, load_settings
from vhtk.core.device import Device
from vhtk.core.errors import (
    DeviceLost,
    KillSwitchTriggered,
    StateTimeout,
    TaskDefinitionError,
)
from vhtk.core.fsm import (
    DONE,
    RECOVER,
    BaseTask,
    State,
    TaskContext,
    TaskResult,
    TaskStatus,
)
from vhtk.core.recovery import Recovery, RecoveryOutcome
from vhtk.core.safety import Image, Safety
from vhtk.core.vision import Vision
from vhtk.notify import NullNotifier

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


class FakeRecovery:
    """Scripted recovery; records every call. Defaults: everything is OK."""

    def __init__(
        self,
        check_outcomes: list[RecoveryOutcome] | None = None,
        failure_outcomes: list[RecoveryOutcome] | None = None,
    ) -> None:
        self.check_count = 0
        self.failure_calls: list[tuple[str, str]] = []
        self._check = deque(check_outcomes or [])
        self._failure = deque(failure_outcomes or [])

    def check(self) -> RecoveryOutcome:
        self.check_count += 1
        return self._check.popleft() if self._check else RecoveryOutcome.OK

    def on_state_failure(self, state: str, reason: str) -> RecoveryOutcome:
        self.failure_calls.append((state, reason))
        return self._failure.popleft() if self._failure else RecoveryOutcome.RECOVERED


class FakeVision:
    """Only what the FSM itself touches: abort-evidence capture."""

    def capture_screen(self) -> Image:
        return np.zeros((10, 10, 3), dtype=np.uint8)


class RecordingNotifier(NullNotifier):
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.photos: list[str] = []

    def send_text(self, text: str) -> None:
        self.texts.append(text)

    def send_photo(self, image: Image, caption: str) -> None:
        self.photos.append(caption)


def make_settings(max_recover: int) -> Settings:
    settings = load_settings(SETTINGS_PATH)
    safety = settings.safety.model_copy(update={"max_recover_attempts": max_recover})
    return settings.model_copy(update={"safety": safety})


def make_ctx(
    tmp_path: Path,
    recovery: FakeRecovery | None = None,
    max_recover: int = 3,
) -> tuple[TaskContext, FakeRecovery, RecordingNotifier]:
    settings = make_settings(max_recover)
    notifier = RecordingNotifier()
    safety = Safety(
        settings.safety,
        notifier,
        evidence_dir=tmp_path / "dry_run",
        kill_switch_path=tmp_path / "STOP",
    )
    rec = recovery or FakeRecovery()
    ctx = TaskContext(
        device=cast(Device, object()),
        vision=cast(Vision, FakeVision()),
        safety=safety,
        recovery=cast(Recovery, rec),
        notifier=notifier,
        settings=settings,
        account=None,
        log_dir=tmp_path / "logs",
    )
    return ctx, rec, notifier


def build_task(ctx: TaskContext, state_list: Sequence[State], initial: str) -> BaseTask:
    class DemoTask(BaseTask):
        name = "demo"

        def states(self) -> Sequence[State]:
            return state_list

        def initial_state(self) -> str:
            return initial

    return DemoTask(ctx)


def run_task(ctx: TaskContext, state_list: Sequence[State], initial: str = "A") -> TaskResult:
    return build_task(ctx, state_list, initial).run()


# ---------------------------------------------------------------------------
# Happy path & transition rules
# ---------------------------------------------------------------------------


def test_happy_path_runs_states_in_order(tmp_path: Path) -> None:
    ctx, rec, _ = make_ctx(tmp_path)
    order: list[str] = []

    def do_a() -> str:
        order.append("A")
        return "B"

    def do_b() -> str:
        order.append("B")
        return DONE

    result = run_task(
        ctx,
        [
            State("A", do_a, 5.0, frozenset({"B"})),
            State("B", do_b, 5.0, frozenset({DONE})),
        ],
    )

    assert result.status is TaskStatus.SUCCESS
    assert result.final_state == "B"
    assert order == ["A", "B"]
    # recovery.check chạy TRƯỚC mỗi state (quy tắc #3): 2 state → 2 lần
    assert rec.check_count == 2


def test_undeclared_transition_is_a_definition_error(tmp_path: Path) -> None:
    ctx, _, _ = make_ctx(tmp_path)

    task = build_task(
        ctx,
        [
            State("A", lambda: "C", 5.0, frozenset({"B"})),
            State("B", lambda: DONE, 5.0, frozenset({DONE})),
        ],
        "A",
    )
    with pytest.raises(TaskDefinitionError, match="undeclared next state 'C'"):
        task.run()


def test_done_must_be_declared(tmp_path: Path) -> None:
    ctx, _, _ = make_ctx(tmp_path)

    task = build_task(
        ctx,
        [
            State("A", lambda: DONE, 5.0, frozenset({"B"})),  # DONE không khai báo
            State("B", lambda: DONE, 5.0, frozenset({DONE})),
        ],
        "A",
    )
    with pytest.raises(TaskDefinitionError, match="may not finish with DONE"):
        task.run()


@pytest.mark.parametrize(
    ("state_list", "initial", "match"),
    [
        (
            [State(RECOVER, lambda: DONE, 5.0, frozenset({DONE}))],
            RECOVER,
            "reserved",
        ),
        (
            [
                State("A", lambda: DONE, 5.0, frozenset({DONE})),
                State("A", lambda: DONE, 5.0, frozenset({DONE})),
            ],
            "A",
            "duplicate",
        ),
        (
            [State("A", lambda: DONE, 5.0, frozenset({DONE, "GHOST"}))],
            "A",
            "unknown next states",
        ),
        (
            [State("A", lambda: DONE, 5.0, frozenset({DONE}))],
            "NOPE",
            "initial state",
        ),
        (
            [State("A", lambda: "A", 5.0, frozenset({"A"}))],
            "A",
            "no state that can reach DONE",
        ),
    ],
)
def test_invalid_graphs_rejected_at_construction(
    tmp_path: Path, state_list: Sequence[State], initial: str, match: str
) -> None:
    ctx, _, _ = make_ctx(tmp_path)
    with pytest.raises(TaskDefinitionError, match=match):
        build_task(ctx, state_list, initial)


# ---------------------------------------------------------------------------
# Recovery routing
# ---------------------------------------------------------------------------


def test_recoverable_error_retries_state(tmp_path: Path) -> None:
    ctx, rec, _ = make_ctx(tmp_path)
    calls = {"A": 0}

    def flaky_a() -> str:
        calls["A"] += 1
        if calls["A"] == 1:
            raise StateTimeout("button not found")
        return DONE

    result = run_task(ctx, [State("A", flaky_a, 5.0, frozenset({DONE}))])

    assert result.status is TaskStatus.SUCCESS
    assert result.recover_count == 1
    assert calls["A"] == 2
    assert rec.failure_calls == [("A", "StateTimeout: button not found")]


def test_gives_up_after_max_recover_attempts(tmp_path: Path) -> None:
    ctx, rec, _ = make_ctx(tmp_path, max_recover=2)

    def always_fails() -> str:
        raise StateTimeout("never appears")

    result = run_task(ctx, [State("A", always_fails, 5.0, frozenset({DONE}))])

    assert result.status is TaskStatus.ABORTED
    assert result.final_state == "A"
    assert "gave up after 2 recover attempts" in result.reason
    assert len(rec.failure_calls) == 2


def test_successful_state_resets_consecutive_recover_budget(tmp_path: Path) -> None:
    ctx, _, _ = make_ctx(tmp_path, max_recover=1)
    calls = {"A": 0, "B": 0}

    def flaky_a() -> str:
        calls["A"] += 1
        if calls["A"] == 1:
            raise StateTimeout("a")
        return "B"

    def flaky_b() -> str:
        calls["B"] += 1
        if calls["B"] == 1:
            raise StateTimeout("b")
        return DONE

    result = run_task(
        ctx,
        [
            State("A", flaky_a, 5.0, frozenset({"B"})),
            State("B", flaky_b, 5.0, frozenset({DONE})),
        ],
    )

    # mỗi state fail 1 lần, quota 1 lần/state — nhờ reset sau state thành công
    assert result.status is TaskStatus.SUCCESS
    assert result.recover_count == 2


def test_handler_may_request_recover_explicitly(tmp_path: Path) -> None:
    ctx, rec, _ = make_ctx(tmp_path)
    calls = {"A": 0}

    def do_a() -> str:
        calls["A"] += 1
        return RECOVER if calls["A"] == 1 else DONE

    result = run_task(ctx, [State("A", do_a, 5.0, frozenset({DONE}))])

    assert result.status is TaskStatus.SUCCESS
    assert rec.failure_calls == [("A", "requested by state 'A'")]


def test_unrecoverable_error_aborts_without_recovery(tmp_path: Path) -> None:
    ctx, rec, notifier = make_ctx(tmp_path)

    def lose_device() -> str:
        raise DeviceLost("adb gone")

    result = run_task(ctx, [State("A", lose_device, 5.0, frozenset({DONE}))])

    assert result.status is TaskStatus.ABORTED
    assert "DeviceLost" in result.reason
    assert rec.failure_calls == []  # không recover — ABORT thẳng
    # bằng chứng abort: ảnh đã lưu + đã báo người
    assert len(list((tmp_path / "logs" / "abort").glob("*.png"))) == 1
    assert len(notifier.photos) == 1


# ---------------------------------------------------------------------------
# Relogin & kill-switch
# ---------------------------------------------------------------------------


def test_relogin_detected_before_state(tmp_path: Path) -> None:
    rec = FakeRecovery(check_outcomes=[RecoveryOutcome.RELOGIN])
    ctx, _, _ = make_ctx(tmp_path, recovery=rec)
    ran: list[str] = []

    def do_a() -> str:
        ran.append("A")
        return DONE

    result = run_task(ctx, [State("A", do_a, 5.0, frozenset({DONE}))])

    assert result.status is TaskStatus.RELOGIN_REQUIRED
    assert ran == []  # handler chưa được chạy


def test_relogin_detected_during_deep_recovery(tmp_path: Path) -> None:
    rec = FakeRecovery(failure_outcomes=[RecoveryOutcome.RELOGIN])
    ctx, _, _ = make_ctx(tmp_path, recovery=rec)

    def fails() -> str:
        raise StateTimeout("x")

    result = run_task(ctx, [State("A", fails, 5.0, frozenset({DONE}))])

    assert result.status is TaskStatus.RELOGIN_REQUIRED
    assert result.final_state == "A"


def test_recovery_resume_state_override(tmp_path: Path) -> None:
    """Task ghi đè recovery_resume_state: sau recover quay về state chỉ định."""
    ctx, _, _ = make_ctx(tmp_path)
    order: list[str] = []
    fail_once = {"done": False}

    def do_home() -> str:
        order.append("HOME")
        return "WORK"

    def do_work() -> str:
        order.append("WORK")
        if not fail_once["done"]:
            fail_once["done"] = True
            raise StateTimeout("flaky")
        return DONE

    state_list = [
        State("HOME", do_home, 5.0, frozenset({"WORK"})),
        State("WORK", do_work, 5.0, frozenset({DONE})),
    ]

    class FlowTask(BaseTask):
        name = "flow"

        def states(self) -> Sequence[State]:
            return state_list

        def initial_state(self) -> str:
            return "HOME"

        def recovery_resume_state(self, failed_state: str) -> str:
            return "HOME"  # luôn quay về state tự định tuyến

    result = FlowTask(ctx).run()

    assert result.status is TaskStatus.SUCCESS
    # WORK fail 1 lần -> recover -> resume HOME (không phải retry WORK)
    assert order == ["HOME", "WORK", "HOME", "WORK"]


def test_relogin_is_normal_lets_login_task_continue(tmp_path: Path) -> None:
    """Task có relogin_is_normal=True (login) không bị RELOGIN đá ra ngoài."""
    rec = FakeRecovery(check_outcomes=[RecoveryOutcome.RELOGIN])
    ctx, _, _ = make_ctx(tmp_path, recovery=rec)
    state_list = [State("A", lambda: DONE, 5.0, frozenset({DONE}))]

    class LoginLikeTask(BaseTask):
        name = "login-like"
        relogin_is_normal = True

        def states(self) -> Sequence[State]:
            return state_list

        def initial_state(self) -> str:
            return "A"

    result = LoginLikeTask(ctx).run()

    assert result.status is TaskStatus.SUCCESS


def test_kill_switch_between_states(tmp_path: Path) -> None:
    ctx, _, _ = make_ctx(tmp_path)
    ran: list[str] = []

    def do_a() -> str:
        ran.append("A")
        (tmp_path / "STOP").touch()  # người ra lệnh dừng giữa chừng
        return "B"

    def do_b() -> str:
        ran.append("B")
        return DONE

    result = run_task(
        ctx,
        [
            State("A", do_a, 5.0, frozenset({"B"})),
            State("B", do_b, 5.0, frozenset({DONE})),
        ],
    )

    assert result.status is TaskStatus.KILLED
    assert ran == ["A"]  # B không bao giờ chạy — dừng sạch ở ranh giới state


def test_kill_switch_raised_inside_handler(tmp_path: Path) -> None:
    ctx, rec, _ = make_ctx(tmp_path)

    def do_a() -> str:
        raise KillSwitchTriggered("STOP appeared mid-action")

    result = run_task(ctx, [State("A", do_a, 5.0, frozenset({DONE}))])

    assert result.status is TaskStatus.KILLED
    assert rec.failure_calls == []  # kill-switch không phải lỗi cần recover
