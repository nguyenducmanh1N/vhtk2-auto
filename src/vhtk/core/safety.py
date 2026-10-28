"""Safety gate for irreversible actions: dry_run, confirm_mode, session limits.

``Safety.guard_irreversible`` is the ONE gate every sell/buy/trade action must
pass through (rule #4 of CLAUDE.md). Task code never checks ``dry_run`` or
``confirm_mode`` itself — it asks the gate and obeys the answer.

Order of checks per action:
1. kill-switch (human stop wins over everything)
2. session limit on EXECUTED irreversible actions (looping-bug fuse)
3. ``dry_run`` -> save evidence screenshot + log, action is skipped
4. ``confirm_mode`` -> ask a human via the notifier; timeout counts as denial
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TypeAlias

import cv2
import numpy as np
import numpy.typing as npt
import structlog

from vhtk.config import SafetySettings
from vhtk.core.errors import KillSwitchTriggered, SafetyLimitExceeded
from vhtk.notify import Notifier

log = structlog.get_logger(__name__)

Image: TypeAlias = npt.NDArray[np.uint8]


def kill_switch_files(base: Path, stem: str | None = None) -> tuple[Path, ...]:
    """Kill-switch file set for one worker: global STOP plus per-account STOP.

    Multi-instance rule (W2): the global file (``STOP``) stops EVERY worker;
    ``STOP.<stem>`` stops only the worker running that account. A worker
    checks both; single-account tools pass ``stem=None`` and check only the
    global file.
    """
    if stem is None:
        return (base,)
    return (base, base.with_name(f"{base.name}.{stem}"))


class Safety:
    """Session-scoped safety state: kill-switch, dry_run, confirm, counters.

    One instance per task worker/session; the irreversible-action counter is
    intentionally NOT shared between instances.

    Args:
        cfg: safety section of settings.yaml.
        notifier: used for confirm_mode prompts and limit warnings.
        evidence_dir: where dry_run evidence screenshots are written
            (e.g. ``logs/dry_run``); created on first use.
        kill_switch_path: kill-switch file location override (tests);
            defaults to ``cfg.kill_switch_file`` relative to the CWD.
        stem: account stem this worker runs; when given, the per-account
            kill-switch file ``STOP.<stem>`` is honored as well.
    """

    def __init__(
        self,
        cfg: SafetySettings,
        notifier: Notifier,
        evidence_dir: Path,
        kill_switch_path: Path | None = None,
        stem: str | None = None,
    ) -> None:
        self._cfg = cfg
        self._notifier = notifier
        self._evidence_dir = evidence_dir
        base = kill_switch_path or Path(cfg.kill_switch_file)
        self._kill_switches = kill_switch_files(base, stem)
        self._executed_count = 0

    @property
    def executed_irreversible(self) -> int:
        """How many irreversible actions were actually EXECUTED this session."""
        return self._executed_count

    def check_kill_switch(self) -> None:
        """Raise if the kill-switch file exists.

        Raises:
            KillSwitchTriggered: a STOP file (global or per-account) is present.
        """
        for path in self._kill_switches:
            if path.exists():
                raise KillSwitchTriggered(f"kill-switch file present: {path}")

    def guard_irreversible(
        self,
        action: str,
        detail: str,
        screenshot: Image | None = None,
    ) -> bool:
        """Decide whether an irreversible action (sell/buy/trade) may run NOW.

        Args:
            action: machine-readable action name, e.g. ``"sell_item"``.
            detail: human-readable description shown in logs/confirm prompts,
                e.g. ``"sell 'Thiết Kiếm' quality=trắng"``.
            screenshot: current frame as evidence; saved in dry_run mode and
                attached to confirm prompts.

        Returns:
            ``True`` — perform the action immediately after this call.
            ``False`` — action skipped (dry_run) or denied (confirm); task
            must carry on WITHOUT performing it.

        Raises:
            KillSwitchTriggered: the STOP file is present.
            SafetyLimitExceeded: executing would exceed
                ``max_irreversible_per_session`` (almost always a loop bug).
        """
        self.check_kill_switch()
        if self._executed_count >= self._cfg.max_irreversible_per_session:
            raise SafetyLimitExceeded(
                f"session limit of {self._cfg.max_irreversible_per_session} "
                f"irreversible actions reached (next: {action!r})"
            )
        if self._cfg.dry_run:
            path = self._save_evidence(action, screenshot)
            log.info("dry_run: action skipped", action=action, detail=detail, evidence=path)
            return False
        if self._cfg.confirm_mode:
            approved = self._notifier.wait_confirm(
                f"Confirm {action}: {detail}", screenshot, self._cfg.confirm_timeout_seconds
            )
            if not approved:
                log.warning("action denied by confirm", action=action, detail=detail)
                return False
        self._executed_count += 1
        log.info(
            "irreversible action approved",
            action=action,
            detail=detail,
            session_count=self._executed_count,
        )
        return True

    def _save_evidence(self, action: str, screenshot: Image | None) -> str | None:
        if screenshot is None:
            return None
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        # timestamp thực (không phải monotonic) để đối chiếu với log JSON lines
        name = f"{time.strftime('%Y%m%d_%H%M%S')}_{action}.png"
        path = self._evidence_dir / name
        cv2.imwrite(str(path), screenshot)
        return str(path)
