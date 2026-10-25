"""State machine base for every business task (rule #2 of CLAUDE.md).

A task subclasses ``BaseTask`` and declares its states: each ``State`` has a
name, a handler, a time budget and the set of legal next states. The runner
(``BaseTask.run``) owns the loop and enforces, for every single state:

1. kill-switch check at the state boundary (human stop wins over everything);
2. ``recovery.check()`` BEFORE the state handler runs (rule #3);
3. transition validation — a handler returning an undeclared state is a
   programming error (``TaskDefinitionError``), never silently followed.

Failure routing:
- recoverable screen trouble (``TaskError``, ``VisionError``, transient
  ``ADBError``) sends the task to the built-in RECOVER state, which runs the
  deep recovery pass and retries the failed state up to
  ``safety.max_recover_attempts`` consecutive times;
- unrecoverable conditions (``DeviceLost``, ``RateLimited``,
  ``SafetyLimitExceeded``) go straight to ABORT;
- being thrown back to the login screen ends the run with
  ``RELOGIN_REQUIRED`` so the orchestrator can rerun the login task first.
"""

from __future__ import annotations

import contextlib
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

import cv2
import structlog

from vhtk.config import AccountConfig, Settings
from vhtk.core.device import Device
from vhtk.core.errors import (
    ADBError,
    DeviceLost,
    KillSwitchTriggered,
    RateLimited,
    SafetyLimitExceeded,
    TaskDefinitionError,
    TaskError,
)
from vhtk.core.recovery import Recovery, RecoveryOutcome
from vhtk.core.safety import Safety
from vhtk.core.vision import Vision, VisionError
from vhtk.notify import Notifier

log = structlog.get_logger(__name__)

# Tên state dành riêng cho runner — task KHÔNG được khai báo state trùng tên,
# nhưng handler được phép TRẢ VỀ chúng (DONE phải khai trong next_states;
# RECOVER/ABORT luôn hợp lệ ngầm định).
DONE = "DONE"
RECOVER = "RECOVER"
ABORT = "ABORT"
RESERVED_STATES = frozenset({DONE, RECOVER, ABORT})

Handler: TypeAlias = Callable[[], str]


class TaskStatus(Enum):
    """Final outcome of one task run."""

    SUCCESS = "success"
    ABORTED = "aborted"
    KILLED = "killed"  # người ra lệnh dừng (kill-switch) — không phải lỗi
    RELOGIN_REQUIRED = "relogin_required"  # văng login — orchestrator chạy lại login


@dataclass(frozen=True)
class TaskResult:
    """What a task run ended with; consumed by the orchestrator."""

    status: TaskStatus
    final_state: str
    reason: str
    recover_count: int


@dataclass(frozen=True)
class State:
    """One task state: name, handler, time budget, legal transitions.

    ``timeout`` is a budget, not a preemption mechanism: handlers own their
    waits (``vision.wait_for`` etc.); the runner logs a warning when a handler
    overruns its budget so slow states are visible in the logs.
    """

    name: str
    handler: Handler
    timeout: float
    next_states: frozenset[str]


@dataclass
class TaskContext:
    """Everything a task handler may touch, injected by the orchestrator."""

    device: Device
    vision: Vision
    safety: Safety
    recovery: Recovery
    notifier: Notifier
    settings: Settings
    account: AccountConfig | None
    log_dir: Path


class BaseTask(ABC):
    """Base class every business task (login, farm, sell...) inherits from.

    Subclasses implement :meth:`states` and :meth:`initial_state`; the state
    graph is validated once at construction time so definition bugs surface
    immediately, not mid-run on a live account.

    Raises:
        TaskDefinitionError: duplicate/reserved state names, transitions to
            unknown states, unknown initial state, or no state that can
            reach ``DONE``.
    """

    #: human-readable task name for logs and notifications
    name: str = "base"

    def __init__(self, ctx: TaskContext) -> None:
        self.ctx = ctx
        self._log = log.bind(task=self.name)
        declared = list(self.states())
        self._states: dict[str, State] = {}
        for st in declared:
            if st.name in RESERVED_STATES:
                raise TaskDefinitionError(f"state name {st.name!r} is reserved")
            if st.name in self._states:
                raise TaskDefinitionError(f"duplicate state name {st.name!r}")
            self._states[st.name] = st
        known = set(self._states) | RESERVED_STATES
        for st in declared:
            unknown = st.next_states - known
            if unknown:
                raise TaskDefinitionError(
                    f"state {st.name!r} declares unknown next states: {sorted(unknown)}"
                )
        if self.initial_state() not in self._states:
            raise TaskDefinitionError(f"initial state {self.initial_state()!r} is not declared")
        if not any(DONE in st.next_states for st in declared):
            raise TaskDefinitionError(f"task {self.name!r} has no state that can reach DONE")

    @abstractmethod
    def states(self) -> Sequence[State]:
        """Declare every state of this task."""

    @abstractmethod
    def initial_state(self) -> str:
        """Name of the state the task starts in."""

    # -- runner ----------------------------------------------------------------

    def run(self) -> TaskResult:
        """Execute the task until SUCCESS / ABORTED / KILLED / RELOGIN_REQUIRED."""
        current = self.initial_state()
        failed_state: str | None = None
        reason = ""
        consecutive_recovers = 0
        total_recovers = 0
        self._log.info("task started", initial=current)

        while True:
            # kill-switch ở ranh giới state: điểm dừng sạch gần nhất
            try:
                self.ctx.safety.check_kill_switch()
            except KillSwitchTriggered as exc:
                self._log.warning("task killed by kill-switch", state=current)
                return TaskResult(TaskStatus.KILLED, current, str(exc), total_recovers)

            if current == ABORT:
                self._on_abort(failed_state, reason)
                return TaskResult(
                    TaskStatus.ABORTED, failed_state or ABORT, reason, total_recovers
                )

            if current == RECOVER:
                consecutive_recovers += 1
                total_recovers += 1
                limit = self.ctx.settings.safety.max_recover_attempts
                if consecutive_recovers > limit:
                    reason = f"{reason} (gave up after {limit} recover attempts)"
                    current = ABORT
                    continue
                self._log.warning(
                    "recovering",
                    failed_state=failed_state,
                    attempt=consecutive_recovers,
                    reason=reason,
                )
                outcome = self.ctx.recovery.on_state_failure(failed_state or "?", reason)
                if outcome is RecoveryOutcome.RELOGIN:
                    return TaskResult(
                        TaskStatus.RELOGIN_REQUIRED, failed_state or RECOVER, reason, total_recovers
                    )
                current = failed_state or self.initial_state()
                continue

            # recovery chạy TRƯỚC mọi state (quy tắc #3)
            outcome = self.ctx.recovery.check()
            if outcome is RecoveryOutcome.RELOGIN:
                return TaskResult(
                    TaskStatus.RELOGIN_REQUIRED, current, "thrown back to login", total_recovers
                )

            state = self._states[current]
            started = time.monotonic()
            try:
                nxt = state.handler()
            except KillSwitchTriggered as exc:
                self._log.warning("task killed by kill-switch", state=current)
                return TaskResult(TaskStatus.KILLED, current, str(exc), total_recovers)
            except (DeviceLost, RateLimited, SafetyLimitExceeded) as exc:
                failed_state = current
                reason = f"{type(exc).__name__}: {exc}"
                current = ABORT
                continue
            except (TaskError, ADBError, VisionError) as exc:
                failed_state = current
                reason = f"{type(exc).__name__}: {exc}"
                current = RECOVER
                continue

            elapsed = time.monotonic() - started
            if elapsed > state.timeout:
                self._log.warning(
                    "state over time budget",
                    state=current,
                    elapsed_s=round(elapsed, 1),
                    budget_s=state.timeout,
                )

            if nxt == DONE:
                if DONE not in state.next_states:
                    raise TaskDefinitionError(f"state {current!r} may not finish with DONE")
                self._log.info("task finished", final_state=current, recovers=total_recovers)
                return TaskResult(TaskStatus.SUCCESS, current, "", total_recovers)
            if nxt in (RECOVER, ABORT):
                failed_state = current
                reason = f"requested by state {current!r}"
                current = nxt
                continue
            if nxt not in state.next_states:
                raise TaskDefinitionError(
                    f"state {current!r} returned undeclared next state {nxt!r}"
                )

            self._log.debug("state transition", frm=current, to=nxt)
            consecutive_recovers = 0  # state hoàn tất → reset quota recover liên tiếp
            failed_state = None
            reason = ""
            current = nxt

    # -- internals ---------------------------------------------------------------

    def _on_abort(self, failed_state: str | None, reason: str) -> None:
        """Best-effort evidence collection; must never raise."""
        self._log.error("task aborted", state=failed_state, reason=reason)
        frame = None
        with contextlib.suppress(Exception):
            frame = self.ctx.vision.capture_screen()
        if frame is None:
            with contextlib.suppress(Exception):
                self.ctx.notifier.send_text(f"Task {self.name!r} aborted: {reason}")
            return
        with contextlib.suppress(Exception):
            abort_dir = self.ctx.log_dir / "abort"
            abort_dir.mkdir(parents=True, exist_ok=True)
            path = abort_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{self.name}.png"
            cv2.imwrite(str(path), frame)
            self._log.info("abort evidence saved", path=str(path))
        with contextlib.suppress(Exception):
            self.ctx.notifier.send_photo(frame, f"Task {self.name!r} aborted: {reason}")
