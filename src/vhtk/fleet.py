"""Fleet control: per-device op locks, desired state, watchdog, system-wide ops.

Sits on top of :class:`~vhtk.orchestrator.WorkerSupervisor` (which knows how
to start/stop ONE worker + its LDPlayer instance) and adds what the operator
needs to run a whole farm unattended:

- :class:`OpLocks` — one in-flight lifecycle op per account. Starting a
  device blocks for minutes (instance boot) and the worker only registers
  after that; without a lock the GUI showed "Mở & đăng nhập" again mid-boot
  and a second click launched it twice.
- :class:`DesiredState` — what the operator WANTS per account
  ("running"/"stopped"), persisted to ``<workers_dir>/desired.json`` so the
  watchdog keeps its intent across GUI restarts. Only "running" accounts are
  ever auto-restarted; pressing Dừng means hands off.
- :class:`Watchdog` — notices a worker that died (abort/crash) or hung (no
  heartbeat), marks it failed for the GUI (red card) and restarts it with
  growing backoff, escalating to an instance reboot from the 2nd consecutive
  failure; gives up after ``max_restarts_per_hour``. Failures CLAUDE.md says
  a human must handle (config/template missing, wrong server, safety cap,
  bag-full loop...) are never retried.
- :class:`FleetController` — "Chạy / Chạy lại / Dừng tất cả" with progress.

No game logic here; every side effect goes through the supervisor.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog

from vhtk.config import WatchdogSettings

log = structlog.get_logger(__name__)

Desired = Literal["running", "stopped"]


class SupervisorLike(Protocol):
    """The subset of WorkerSupervisor the fleet layer drives."""

    def start(self, stem: str) -> int: ...
    def stop(self, stem: str) -> None: ...
    def is_alive(self, stem: str) -> bool: ...


# ---------------------------------------------------------------------------
# Per-account op lock
# ---------------------------------------------------------------------------


class OpLocks:
    """At most ONE lifecycle op (start/stop/restart/...) per account at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ops: dict[str, str] = {}

    def try_begin(self, stem: str, op: str) -> str | None:
        """Claim ``stem`` for ``op``; returns the op already running, or None."""
        with self._lock:
            busy = self._ops.get(stem)
            if busy is not None:
                return busy
            self._ops[stem] = op
            return None

    def end(self, stem: str) -> None:
        with self._lock:
            self._ops.pop(stem, None)

    def current(self, stem: str) -> str | None:
        with self._lock:
            return self._ops.get(stem)


# ---------------------------------------------------------------------------
# Desired state (persisted)
# ---------------------------------------------------------------------------


class DesiredState:
    """``{stem: "running"|"stopped"}`` persisted as JSON; unknown = stopped."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Desired]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if v in ("running", "stopped")}

    def get(self, stem: str) -> Desired:
        with self._lock:
            return self._read().get(stem, "stopped")

    def all(self) -> dict[str, Desired]:
        with self._lock:
            return self._read()

    def set(self, stem: str, value: Desired) -> None:
        with self._lock:
            data = self._read()
            data[stem] = value
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)


# ---------------------------------------------------------------------------
# Failure classification (pure)
# ---------------------------------------------------------------------------

# lý do do TASK chủ động ABORT ở bước kiểm tra cấu hình/template — chạy lại
# cũng hỏng y như cũ, phải có người sửa config / chụp template
_HUMAN_ABORT_STATES = ("'VERIFY_HUD'",)


def needs_human(state: dict[str, Any]) -> bool:
    """Whether a dead worker's last state is one a human must fix first.

    Reads the worker state file fields ``status``/``task``/``reason``.
    Everything else (network loss, stuck travel, timeouts, crashes, lost ADB)
    is considered transient and retried by the watchdog.
    """
    status = str(state.get("status", ""))
    task = str(state.get("task", ""))
    reason = str(state.get("reason", ""))
    if status == "stopped":
        # bag_full_loop (bán xong túi vẫn đầy), max_cycles (chủ ý dừng)
        return True
    if status == "crashed" and ("invalid config" in reason or "config file" in reason):
        return True
    if reason.startswith("SafetyLimitExceeded"):
        return True
    if reason.startswith("requested by state"):
        if task == "login":
            # sai server / màn tạo nhân vật / slot không tồn tại
            return True
        if any(s in reason for s in _HUMAN_ABORT_STATES):
            return True
    return False


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


@dataclass
class Health:
    """Watchdog bookkeeping for one account (exposed to the GUI)."""

    failures: list[float] = field(default_factory=list)  # thời điểm các lần hỏng
    consecutive: int = 0
    next_retry_at: float | None = None
    gave_up: bool = False
    last_reason: str = ""
    alive_since: float | None = None
    # lần hỏng hiện tại đã được ghi nhận chưa (một xác chết chỉ đếm một lần)
    pending: bool = False

    def view(self, now: float, limit: int) -> dict[str, Any]:
        return {
            "failed": self.pending or self.gave_up,
            "gave_up": self.gave_up,
            "reason": self.last_reason,
            "consecutive": self.consecutive,
            "failures_last_hour": len([t for t in self.failures if now - t < 3600]),
            "limit_per_hour": limit,
            "retry_in": (
                max(0.0, round(self.next_retry_at - now, 1))
                if self.next_retry_at is not None and not self.gave_up
                else None
            ),
        }


WatchdogActionKind = Literal["start", "reboot", "kill_hung"]


@dataclass(frozen=True)
class WatchdogAction:
    stem: str
    kind: WatchdogActionKind


class Watchdog:
    """Decide (pure) and execute (via supervisor) automatic recoveries.

    Args:
        cfg: ``orchestrator.watchdog`` settings.
        clock: wall clock (state-file heartbeats are ``time.time()`` values).
    """

    def __init__(self, cfg: WatchdogSettings, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self._clock = clock
        self._health: dict[str, Health] = {}
        self._lock = threading.Lock()

    def health(self, stem: str) -> Health:
        with self._lock:
            return self._health.setdefault(stem, Health())

    def reset(self, stem: str) -> None:
        """Operator took over (manual start/stop): forget failures, un-give-up."""
        with self._lock:
            self._health[stem] = Health()

    def view(self, stem: str) -> dict[str, Any]:
        return self.health(stem).view(self._clock(), self.cfg.max_restarts_per_hour)

    # -- pure decision ------------------------------------------------------------

    def decide(
        self,
        stem: str,
        *,
        desired: Desired,
        busy: bool,
        alive: bool,
        state: dict[str, Any],
    ) -> WatchdogAction | None:
        """One account, one tick: what (if anything) to do now.

        Mutates only this account's :class:`Health`; never touches processes.
        """
        if not self.cfg.enabled or desired != "running" or busy:
            return None
        now = self._clock()
        h = self.health(stem)
        if alive:
            if h.alive_since is None:
                h.alive_since = now
            hb = state.get("heartbeat_at")
            started = h.alive_since
            # treo: còn sống nhưng lâu không chụp màn hình (adb/game đơ).
            # Chỉ xét khi đã từng có nhịp tim — worker cũ không gửi thì thôi
            if (
                isinstance(hb, (int, float))
                and now - float(hb) > self.cfg.hang_timeout
                and now - started > self.cfg.hang_timeout
            ):
                self._record_failure(h, now, "hung: no heartbeat for "
                                     f"{int(now - float(hb))}s")
                return WatchdogAction(stem, "kill_hung")
            if now - started >= self.cfg.healthy_after:
                h.consecutive = 0  # chạy ổn đủ lâu -> lần hỏng sau tính lại từ đầu
            h.pending = False
            return None

        h.alive_since = None
        if h.gave_up:
            return None
        if not h.pending and str(state.get("status", "")) == "killed":
            # dừng bằng kill-switch = người chủ ý dừng (watchdog tự dừng worker
            # treo thì đã ghi nhận hỏng trước -> pending, không rơi vào đây)
            return None
        if not h.pending:
            status = str(state.get("status", ""))
            reason = str(state.get("reason") or "")
            if status in ("running", "") and not reason:
                # tiến trình chết đột ngột (crash cứng/bị kill) — không kịp ghi lý do
                reason = "worker process exited unexpectedly"
            reason = reason or status
            if needs_human(state):
                h.gave_up = True
                h.pending = True
                h.last_reason = reason
                log.error("watchdog: needs a human, not restarting", stem=stem, reason=reason)
                return None
            self._record_failure(h, now, reason)
        if h.gave_up or h.next_retry_at is None or now < h.next_retry_at:
            return None
        h.next_retry_at = None
        kind: WatchdogActionKind = "reboot" if h.consecutive >= 2 else "start"
        return WatchdogAction(stem, kind)

    def _record_failure(self, h: Health, now: float, reason: str) -> None:
        h.failures = [t for t in h.failures if now - t < 3600] + [now]
        h.consecutive += 1
        h.pending = True
        h.last_reason = reason
        if len(h.failures) > self.cfg.max_restarts_per_hour:
            h.gave_up = True
            h.next_retry_at = None
            log.error("watchdog: too many failures, giving up", reason=reason,
                      failures=len(h.failures))
            return
        backoff = self.cfg.backoff_seconds
        delay = backoff[min(h.consecutive - 1, len(backoff) - 1)]
        h.next_retry_at = now + delay
        log.warning("watchdog: worker failed, retry scheduled", reason=reason,
                    consecutive=h.consecutive, retry_in=delay)

    # -- execution ----------------------------------------------------------------

    def execute(self, action: WatchdogAction, sup: SupervisorLike) -> None:
        """Carry out one decided action through the supervisor."""
        log.warning("watchdog: recovering worker", stem=action.stem, kind=action.kind)
        if action.kind in ("reboot", "kill_hung"):
            # stop() dừng sạch -> buộc dừng nếu treo -> tắt instance (khi được
            # quản lý); start() bật lại instance + worker
            sup.stop(action.stem)
        if action.kind == "kill_hung":
            return  # tick kế tiếp thấy chết -> lịch chạy lại như mọi lần hỏng
        sup.start(action.stem)


# ---------------------------------------------------------------------------
# System-wide ops
# ---------------------------------------------------------------------------

SystemOp = Literal["start_all", "stop_all", "restart_all"]


@dataclass
class SystemStatus:
    op: str | None = None
    total: int = 0
    done: int = 0
    current: str = ""
    errors: list[str] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    last_op: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "op": self.op, "total": self.total, "done": self.done, "current": self.current,
            "errors": list(self.errors), "started_at": self.started_at,
            "finished_at": self.finished_at, "last_op": self.last_op,
        }


class FleetController:
    """Run start/stop/restart over many accounts with per-account locking.

    Args:
        locks / desired / watchdog: shared fleet singletons of the server.
        make_supervisor: builds a supervisor bound to the CURRENT settings.
        max_concurrent: ``orchestrator.max_concurrent_instances``.
    """

    def __init__(
        self,
        locks: OpLocks,
        desired: DesiredState,
        watchdog: Watchdog,
        make_supervisor: Callable[[], SupervisorLike],
        max_concurrent: Callable[[], int],
    ) -> None:
        self.locks = locks
        self.desired = desired
        self.watchdog = watchdog
        self._make_supervisor = make_supervisor
        self._max_concurrent = max_concurrent
        self.status = SystemStatus()
        self._lock = threading.Lock()

    def busy(self) -> bool:
        with self._lock:
            return self.status.op is not None

    def begin(self, op: SystemOp, total: int) -> bool:
        with self._lock:
            if self.status.op is not None:
                return False
            self.status = SystemStatus(op=op, total=total, started_at=time.time(),
                                       last_op=self.status.last_op)
            return True

    def run(self, op: SystemOp, farm_stems: list[str], all_stems: list[str]) -> None:
        """Execute ``op`` (call after :meth:`begin`); always clears the busy flag."""
        try:
            if op in ("stop_all", "restart_all"):
                self._stop_all(all_stems)
            if op in ("start_all", "restart_all"):
                self._start_all(farm_stems)
        except Exception as exc:  # luồng nền không được chết im lặng
            log.error("system op crashed", op=op, error=str(exc))
            self.status.errors.append(f"{op}: {exc}")
        finally:
            with self._lock:
                self.status.last_op = op
                self.status.op = None
                self.status.current = ""
                self.status.finished_at = time.time()
            log.info("system op finished", op=op, errors=len(self.status.errors))

    def _stop_all(self, stems: Iterable[str]) -> None:
        stems = list(stems)
        for s in stems:
            self.desired.set(s, "stopped")  # trước tiên: watchdog không được hồi sinh
            self.watchdog.reset(s)
        threads = [threading.Thread(target=self._stop_one, args=(s,), daemon=True)
                   for s in stems]
        self.status.current = "đang dừng tất cả"
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _stop_one(self, stem: str) -> None:
        if self.locks.try_begin(stem, "stop") is not None:
            self.status.errors.append(f"{stem}: đang có thao tác khác, bỏ qua")
            return
        try:
            self._make_supervisor().stop(stem)
        except Exception as exc:
            self.status.errors.append(f"{stem}: {exc}")
        finally:
            self.locks.end(stem)
            with self._lock:
                self.status.done += 1

    def _start_all(self, stems: list[str]) -> None:
        sup = self._make_supervisor()
        limit = self._max_concurrent()
        alive = [s for s in stems if sup.is_alive(s)]
        slots = limit - len(alive)
        for stem in stems:
            if stem in alive:
                self.desired.set(stem, "running")
                with self._lock:
                    self.status.done += 1
                continue
            if slots <= 0:
                self.status.errors.append(
                    f"{stem}: vượt giới hạn {limit} máy chạy cùng lúc, không bật"
                )
                with self._lock:
                    self.status.done += 1
                continue
            self.status.current = stem
            self.desired.set(stem, "running")
            self.watchdog.reset(stem)
            if self.locks.try_begin(stem, "start") is not None:
                self.status.errors.append(f"{stem}: đang có thao tác khác, bỏ qua")
            else:
                try:
                    self._make_supervisor().start(stem)
                    slots -= 1
                except Exception as exc:
                    self.status.errors.append(f"{stem}: {exc}")
                finally:
                    self.locks.end(stem)
            with self._lock:
                self.status.done += 1


# ---------------------------------------------------------------------------
# What the device card shows (pure — GUI only renders it)
# ---------------------------------------------------------------------------

Phase = Literal["busy", "running", "needs_human", "failed", "device_on", "off"]


def device_phase(
    *,
    op: str | None,
    running: bool,
    instance: bool | None,
    health: dict[str, Any],
    state: dict[str, Any],
    desired: Desired,
) -> Phase:
    """One word for the card: precedence busy > running > red > device > off.

    - ``busy``: a lifecycle op is in flight (buttons locked).
    - ``needs_human`` / ``failed``: red; failed = watchdog will retry.
    - ``device_on``: LDPlayer/AVD up but no worker (offer "Chạy worker").
    """
    if op is not None:
        return "busy"
    if running:
        return "running"
    if health.get("gave_up"):
        return "needs_human"
    status = str(state.get("status", ""))
    if health.get("failed") or (desired == "running" and status in ("aborted", "crashed")):
        return "failed"
    if desired != "running" and status in ("aborted", "crashed") and needs_human(state):
        return "needs_human"
    if instance:
        return "device_on"
    return "off"
