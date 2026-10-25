"""Pre-state recovery checks: popups, disconnects, death, relogin, unknown screens.

``Recovery.check()`` runs before EVERY state of every task (rule #3 of
CLAUDE.md): it looks for known-bad conditions on screen and fixes what it can
in place. ``Recovery.on_state_failure()`` is the deeper pass used by the FSM's
RECOVER state after a handler failed — same checks, plus "unknown screen"
evidence collection when nothing is recognized.

Template names used here are UI contracts, like resource IDs; templates that
are not present in the active store are silently skipped, so the module works
(and is testable) with a partial template set.
"""

from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Protocol, TypeAlias

import cv2
import numpy as np
import numpy.typing as npt
import structlog

from vhtk.core.vision import Match, Vision
from vhtk.notify import Notifier

log = structlog.get_logger(__name__)

Image: TypeAlias = npt.NDArray[np.uint8]

# Mỗi nhóm: template nhận diện tình huống -> nút cần bấm để thoát tình huống.
# Thứ tự kiểm tra quan trọng: mất kết nối / chết / văng login phải xét TRƯỚC
# popup thường, vì các màn đó cũng có nút đóng chung chung.
DISCONNECT_MARKERS: tuple[str, ...] = ("common/disconnected",)
RECONNECT_BUTTONS: tuple[str, ...] = ("common/btn_reconnect", "common/btn_confirm")
REVIVE_BUTTONS: tuple[str, ...] = ("common/btn_revive",)
LOGIN_MARKERS: tuple[str, ...] = ("login/title", "login/btn_enter")
POPUP_CLOSE_BUTTONS: tuple[str, ...] = ("common/btn_close", "common/btn_x")

# Chặn vòng lặp "đóng popup nhưng popup không chịu đóng" trong một lần check
_MAX_POPUP_CLOSES = 3


class RecoveryOutcome(Enum):
    """What the recovery pass concluded about the current screen."""

    OK = "ok"  # màn hình sạch, vào state bình thường
    RECOVERED = "recovered"  # có sự cố đã xử lý tại chỗ (đóng popup, hồi sinh...)
    RELOGIN = "relogin"  # bị văng về màn login — cần chạy lại task login
    UNKNOWN = "unknown"  # màn hình lạ, đã lưu ảnh + báo người


class InputDevice(Protocol):
    """The minimal device surface recovery needs (keeps tests device-free)."""

    def tap(self, x: int, y: int) -> None:
        """Tap at screen coordinates."""
        ...


class Recovery:
    """Detect and fix known-bad screen conditions before task states run.

    Args:
        device: tap target for close/revive/reconnect buttons (goes through
            the real input gate, so recovery taps are rate-limited too).
        vision: template matching against the current screen.
        notifier: alerted on unknown screens.
        unknown_dir: where unknown-screen evidence is written
            (e.g. ``logs/unknown``); created on first use.
    """

    def __init__(
        self,
        device: InputDevice,
        vision: Vision,
        notifier: Notifier,
        unknown_dir: Path,
    ) -> None:
        self._device = device
        self._vision = vision
        self._notifier = notifier
        self._unknown_dir = unknown_dir

    def check(self) -> RecoveryOutcome:
        """Quick pass before every state: fix what is fixable in place.

        Returns ``OK`` when the screen shows none of the known-bad markers;
        the task state then has to judge the screen itself.
        """
        frame = self._vision.capture_screen()
        if self._find_any(DISCONNECT_MARKERS, frame) is not None:
            return self._handle_disconnect(frame)
        revive = self._find_any(REVIVE_BUTTONS, frame)
        if revive is not None:
            return self._tap_and_report("revive", revive)
        if self._find_any(LOGIN_MARKERS, frame) is not None:
            log.warning("recovery: thrown back to login screen")
            return RecoveryOutcome.RELOGIN
        return self._close_popups(frame)

    def on_state_failure(self, state: str, reason: str) -> RecoveryOutcome:
        """Deep pass for the FSM's RECOVER state, after a handler failed.

        Runs the same checks as :meth:`check`; when nothing is recognized the
        screen is treated as unknown — evidence is saved to ``unknown_dir``
        and a human is notified. The caller may still retry the failed state
        (the screen may settle by itself); its attempt budget bounds that.
        """
        outcome = self.check()
        if outcome is not RecoveryOutcome.OK:
            return outcome
        frame = self._vision.capture_screen()
        path = self._save_unknown(state, frame)
        log.error("recovery: unknown screen", state=state, reason=reason, evidence=path)
        self._notifier.send_photo(frame, f"Unknown screen in state {state!r}: {reason}")
        return RecoveryOutcome.UNKNOWN

    # -- internals -----------------------------------------------------------

    def _find_any(self, names: tuple[str, ...], frame: Image) -> Match | None:
        for name in names:
            if name not in self._vision.store:
                continue
            m = self._vision.find(name, screenshot=frame)
            if m is not None:
                return m
        return None

    def _handle_disconnect(self, frame: Image) -> RecoveryOutcome:
        log.warning("recovery: disconnect dialog on screen")
        button = self._find_any(RECONNECT_BUTTONS, frame)
        if button is not None:
            self._device.tap(*button.center)
        # không có nút bấm được thì cũng coi như đã xử lý: game thường tự
        # reconnect, vòng check kế tiếp sẽ thấy kết quả (login screen hoặc OK)
        return RecoveryOutcome.RECOVERED

    def _tap_and_report(self, what: str, match: Match) -> RecoveryOutcome:
        log.warning(f"recovery: {what} button on screen, tapping", score=round(match.score, 3))
        self._device.tap(*match.center)
        return RecoveryOutcome.RECOVERED

    def _close_popups(self, frame: Image) -> RecoveryOutcome:
        closed = 0
        current = frame
        for _ in range(_MAX_POPUP_CLOSES):
            button = self._find_any(POPUP_CLOSE_BUTTONS, current)
            if button is None:
                break
            log.info("recovery: closing popup", at=button.center)
            self._device.tap(*button.center)
            closed += 1
            current = self._vision.capture_screen()
        return RecoveryOutcome.RECOVERED if closed > 0 else RecoveryOutcome.OK

    def _save_unknown(self, state: str, frame: Image) -> str:
        self._unknown_dir.mkdir(parents=True, exist_ok=True)
        path = self._unknown_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{state}.png"
        cv2.imwrite(str(path), frame)
        return str(path)
