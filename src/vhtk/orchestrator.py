"""Multi-instance orchestration (W2): one worker PROCESS per LDPlayer instance.

Two parts, kept separate on purpose:

- :class:`WorkerSupervisor` — process + instance lifecycle. Start = (optionally)
  ``ldconsole launch`` + wait for Android boot + resolve the ADB serial, then
  spawn ``python -m vhtk.worker --account <yaml>`` and record the PID in the
  worker registry. Stop = per-account kill-switch file ``STOP.<stem>`` first
  (clean stop at a state boundary), process termination only as escalation.
  On the Mac dev box (``orchestrator.manage_instances: false``) it manages
  processes only and never touches ldconsole — same code, different config.

- :class:`MainScheduler` — pure logic deciding WHEN to open the main (gold
  receiver) instance: batches several farm accounts whose gold threshold
  requests arrive close together into ONE main session, and respects
  ``max_concurrent_instances`` by asking to pause one farm worker first.
  It only returns actions; the caller executes them. (W3 wires the actual
  trade; until then the GUI/CLI uses the supervisor directly.)

No game logic lives here — workers decide what to do in-game.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias

import structlog

from vhtk.config import AccountConfig, Settings, load_account
from vhtk.config_writer import write_account
from vhtk.core.errors import VhtkError
from vhtk.core.ldplayer import LDPlayerManager
from vhtk.core.safety import kill_switch_files
from vhtk.registry import WorkerRegistry
from vhtk.worker import state_file

log = structlog.get_logger(__name__)


class OrchestratorError(VhtkError):
    """A supervisor operation could not be carried out."""


# (stem, account_path, settings_path) -> PID của worker vừa spawn
Spawner: TypeAlias = Callable[[str, Path, Path], int]
# serial -> Android đã boot xong chưa
BootProbe: TypeAlias = Callable[[str], bool]
# (pid, hard) -> dừng tiến trình (terminate; hard=True thì kill)
ProcStopper: TypeAlias = Callable[[int, bool], None]


def _default_spawn(stem: str, account_path: Path, settings_path: Path) -> int:
    out_dir = Path("logs/workers")
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{stem}.out").open("ab") as out:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vhtk.worker",
                "--settings",
                str(settings_path),
                "--account",
                str(account_path),
            ],
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


def _default_boot_probe(serial: str) -> bool:
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "getprop", "sys.boot_completed"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() == "1"


def _default_proc_stop(pid: int, hard: bool) -> None:
    import psutil

    try:
        proc = psutil.Process(pid)
        proc.kill() if hard else proc.terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


class WorkerSupervisor:
    """Start/stop/provision account workers, one OS process each.

    Args:
        settings: validated settings (orchestrator/paths/safety sections used).
        settings_path: settings file passed through to spawned workers.
        ld: LDPlayer manager; required only when
            ``orchestrator.manage_instances`` is true.
        registry: worker registry override (tests).
        spawn: worker-process spawner override (tests).
        boot_probe: Android boot check override (tests).
        proc_stop: process terminate/kill override (tests).
        clock/sleep: time injection (tests).
    """

    ESCALATION_GRACE_S = 10.0

    def __init__(
        self,
        settings: Settings,
        settings_path: Path,
        *,
        ld: LDPlayerManager | None = None,
        registry: WorkerRegistry | None = None,
        spawn: Spawner | None = None,
        boot_probe: BootProbe | None = None,
        proc_stop: ProcStopper | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._settings_path = settings_path
        self._cfg = settings.orchestrator
        self._ld = ld
        self._registry = registry or WorkerRegistry(self._cfg.workers_dir)
        self._spawn = spawn or _default_spawn
        self._boot_probe = boot_probe or _default_boot_probe
        self._proc_stop = proc_stop or _default_proc_stop
        self._clock = clock
        self._sleep = sleep

    # -- queries -----------------------------------------------------------------

    @property
    def registry(self) -> WorkerRegistry:
        return self._registry

    def account_path(self, stem: str) -> Path:
        return self._settings.paths.accounts_dir / f"{stem}.yaml"

    def worker_status(self, stem: str) -> dict[str, Any]:
        """Registry + state-file view of one worker (no process poking)."""
        info = self._registry.info(stem)
        state: dict[str, Any] = {}
        sf = state_file(self._cfg.workers_dir, stem)
        if sf.is_file():
            try:
                raw = json.loads(sf.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    state = raw
            except (OSError, ValueError):
                pass
        return {
            "stem": stem,
            "running": info.alive if info else False,
            "pid": info.pid if info and info.alive else None,
            "state": state,
        }

    # -- lifecycle ---------------------------------------------------------------

    def start(self, stem: str) -> int:
        """Bring the account's device up (config permitting) and spawn its worker.

        Returns:
            PID of the spawned worker.

        Raises:
            OrchestratorError: unknown account, worker already running, or the
                instance cannot be brought to a usable serial.
        """
        path = self.account_path(stem)
        if not path.is_file():
            raise OrchestratorError(f"unknown account {stem!r} ({path})")
        if self._registry.alive(stem):
            raise OrchestratorError(f"worker {stem!r} is already running")
        account = load_account(path)
        serial = self._ensure_instance(stem, path, account)
        # xoá kill-switch theo-acc còn sót từ lần dừng trước
        self._stop_file(stem).unlink(missing_ok=True)
        pid = self._spawn(stem, path, self._settings_path)
        self._registry.register(stem, pid, path)
        log.info("worker started", stem=stem, pid=pid, serial=serial)
        return pid

    def stop(self, stem: str, timeout: float | None = None) -> None:
        """Stop one worker cleanly via its kill-switch; escalate if it hangs.

        Idempotent: a stopped (or never-started) worker is fine. The LDPlayer
        instance is quit afterwards when instance management is on.
        """
        info = self._registry.info(stem)
        if info is None or not info.alive:
            self._registry.unregister(stem)
            self._quit_instance(stem)
            return
        stop_file = self._stop_file(stem)
        stop_file.touch()
        limit = timeout if timeout is not None else self._cfg.stop_timeout
        deadline = self._clock() + limit
        try:
            while self._registry.alive(stem) and self._clock() < deadline:
                self._sleep(0.5)
            if self._registry.alive(stem):
                log.warning("worker ignored kill-switch, terminating", stem=stem, pid=info.pid)
                self._proc_stop(info.pid, False)
                grace = self._clock() + self.ESCALATION_GRACE_S
                while self._registry.alive(stem) and self._clock() < grace:
                    self._sleep(0.5)
                if self._registry.alive(stem):
                    log.error("worker survived terminate, killing", stem=stem, pid=info.pid)
                    self._proc_stop(info.pid, True)
        finally:
            stop_file.unlink(missing_ok=True)
        self._registry.unregister(stem)
        self._quit_instance(stem)
        log.info("worker stopped", stem=stem)

    def restart(self, stem: str) -> int:
        """Stop (if running) then start — ONLY this worker is interrupted."""
        self.stop(stem)
        return self.start(stem)

    def provision(self, stem: str) -> int:
        """Clone the template instance for a NEW account and start its worker.

        The account YAML must already exist (created via the GUI/D1); this
        assigns it a fresh LDPlayer instance cloned from
        ``orchestrator.template_instance_index`` and writes the new
        index/serial back into the YAML.

        Returns:
            PID of the spawned worker.

        Raises:
            OrchestratorError: instance management disabled, no template
                instance configured, or the account is unknown/running.
        """
        if not self._cfg.manage_instances or self._ld is None:
            raise OrchestratorError("provision requires orchestrator.manage_instances: true")
        if self._cfg.template_instance_index is None:
            raise OrchestratorError("orchestrator.template_instance_index is not configured")
        path = self.account_path(stem)
        if not path.is_file():
            raise OrchestratorError(f"unknown account {stem!r} ({path})")
        if self._registry.alive(stem):
            raise OrchestratorError(f"worker {stem!r} is already running")
        account = load_account(path)
        index = self._ld.clone(stem, self._cfg.template_instance_index)
        account = account.model_copy(
            update={"ldplayer": account.ldplayer.model_copy(update={"index": index})}
        )
        write_account(path, account)
        log.info("account provisioned", stem=stem, index=index)
        return self.start(stem)

    # -- internals ---------------------------------------------------------------

    def _stop_file(self, stem: str) -> Path:
        base = Path(self._settings.safety.kill_switch_file)
        return kill_switch_files(base, stem)[1]

    def _ensure_instance(self, stem: str, path: Path, account: AccountConfig) -> str:
        """Running instance with a verified serial; updates the YAML on change."""
        if not self._cfg.manage_instances:
            return account.ldplayer.adb_serial
        if self._ld is None:
            raise OrchestratorError("manage_instances is true but no LDPlayerManager given")
        index = account.ldplayer.index
        if not self._ld.is_running(index):
            self._ld.start(index)
        deadline = self._clock() + self._cfg.boot_timeout
        serial: str | None = None
        while self._clock() < deadline:
            serial = self._ld.resolve_serial(index)
            if serial is not None and self._boot_probe(serial):
                break
            serial = None
            self._sleep(2.0)
        if serial is None:
            raise OrchestratorError(
                f"instance {index} did not boot within {self._cfg.boot_timeout}s"
            )
        if serial != account.ldplayer.adb_serial:
            # serial thực tế khác YAML (cổng bị chiếm) -> ghi lại để worker
            # và các công cụ khác cùng nhìn một nguồn sự thật
            updated = account.model_copy(
                update={"ldplayer": account.ldplayer.model_copy(update={"adb_serial": serial})}
            )
            write_account(path, updated)
            log.warning("adb serial updated in account yaml", stem=stem, serial=serial)
        return serial

    def _quit_instance(self, stem: str) -> None:
        if not self._cfg.manage_instances or self._ld is None:
            return
        path = self.account_path(stem)
        if not path.is_file():
            return
        index = load_account(path).ldplayer.index
        try:
            if self._ld.is_running(index):
                self._ld.stop(index)
        except VhtkError as exc:
            log.warning("cannot quit instance", stem=stem, index=index, error=str(exc))


# ---------------------------------------------------------------------------
# Main-on-demand scheduler (pure logic, consumed in W3)
# ---------------------------------------------------------------------------


ActionKind = Literal["pause_farm", "start_main"]


@dataclass(frozen=True)
class Action:
    """One step the caller must execute, in list order."""

    kind: ActionKind
    stem: str  # farm stem to pause, or the main account's stem


class MainScheduler:
    """Decide when to open the on-demand main instance for gold transfers.

    Pure logic: callers feed it gold-threshold requests (from worker state
    files) and ask :meth:`plan`; it never touches processes itself.
    Batching rule: wait up to ``main_batch_window_seconds`` from the FIRST
    pending request so several farms that hit the threshold close together
    share one main session; respect ``max_concurrent_instances`` by pausing
    one farm that is NOT part of the batch.
    """

    def __init__(self, cfg_max_concurrent: int, batch_window_s: float, main_stem: str) -> None:
        self._max_concurrent = cfg_max_concurrent
        self._window = batch_window_s
        self._main_stem = main_stem
        self._pending: dict[str, float] = {}

    @property
    def pending(self) -> dict[str, float]:
        """Pending gold requests: stem -> monotonic time of first request."""
        return dict(self._pending)

    def note_gold_request(self, stem: str, now: float) -> None:
        """Record that a farm reached its gold threshold (idempotent)."""
        self._pending.setdefault(stem, now)

    def clear(self, stems: list[str]) -> None:
        """Drop requests that were served (transfer done)."""
        for stem in stems:
            self._pending.pop(stem, None)

    def plan(self, now: float, running_farms: set[str], main_running: bool) -> list[Action]:
        """Actions to take now, in order; empty when there is nothing to do."""
        if not self._pending or main_running:
            return []
        first = min(self._pending.values())
        if self._window > 0 and now - first < self._window:
            return []  # còn trong cửa sổ gom chuyến — chờ thêm acc đạt ngưỡng
        actions: list[Action] = []
        if len(running_farms) + 1 > self._max_concurrent:
            # phải nhường slot: tạm dừng một farm KHÔNG nằm trong chuyến giao
            # dịch (farm trong chuyến phải online để đứng điểm hẹn)
            idle = sorted(running_farms - set(self._pending))
            if not idle:
                log.warning("no pausable farm worker; postponing main launch")
                return []
            actions.append(Action(kind="pause_farm", stem=idle[0]))
        actions.append(Action(kind="start_main", stem=self._main_stem))
        return actions
