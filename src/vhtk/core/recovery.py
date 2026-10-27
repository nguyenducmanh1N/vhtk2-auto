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
from typing import Callable, Protocol, TypeAlias

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
DISCONNECT_MARKERS: tuple[str, ...] = (
    "common/disconnected",  # "Liên kết máy chủ thất bại..." (nút Trở lại -> về title)
    "common/patch_net_error",  # "Lỗi mạng khi tải..." của bộ cập nhật (nút Thử lại)
)
RECONNECT_BUTTONS: tuple[str, ...] = (
    "common/btn_retry",  # "Thử lại" — ưu tiên: thử tải tiếp thay vì thoát ra
    "common/btn_reconnect",  # "Trở lại" — về title, flow login tự định tuyến lại
    "common/btn_confirm",
)
REVIVE_BUTTONS: tuple[str, ...] = ("common/btn_revive",)
# hộp "Không lấy được cấu hình cập nhật. Thử lại?" khi mạng yếu lúc game
# khởi động — bấm Thử lại là hành động an toàn duy nhất, mọi task đều gặp
RETRY_BUTTONS: tuple[str, ...] = ("login/btn_retry_config",)
# Hộp thoại NPC cốt truyện (header "Hệ Thống", các lựa chọn đánh dấu ◆):
# lựa chọn DƯỚI CÙNG theo quy ước của game là "từ chối / rời đi"
# ("Ta chỉ đến thăm ngươi thôi!", "Trở lại"...) — an toàn để thoát hội thoại
DIALOG_BULLET: str = "common/dialog_bullet"
# Màn LÀM VIỆC hợp lệ: khi đang mở (vd cửa hiệu NPC — kèm túi đồ), KHÔNG dọn
# popup — nút X của túi/shop không phải popup lạc, đóng nó là phá task đang chạy
WORKSPACE_MARKERS: tuple[str, ...] = (
    "shop/cua_hieu_header",
    # màn bản đồ đang mở là một bước của navigate — X của nó match
    # common/btn_x, để recovery "đóng popup" là phá ngang chuỗi điều hướng
    "map/btn_confirm_coords",
)
LOGIN_MARKERS: tuple[str, ...] = (
    "login/title",
    "login/btn_enter",
    # form "SIGN IN WITH 3CID" — phiên SDK mất là rơi về đây giữa chừng;
    # login task biết điền form này (có credentials trong config)
    "login/signin_title",
)
POPUP_CLOSE_BUTTONS: tuple[str, ...] = (
    "common/btn_close",
    "common/btn_x",
    # hộp thoại hỏi Đồng ý/Huỷ bỏ không có nút X: TỪ CHỐI là lựa chọn an toàn
    # mặc định cho mọi câu hỏi lạ (vd "dùng chế độ tự động sắp xếp?")
    "common/btn_cancel",
)

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
        app_in_foreground: optional zero-arg probe returning whether the game
            app is currently in the foreground; ``None`` skips the check.
    """

    def __init__(
        self,
        device: InputDevice,
        vision: Vision,
        notifier: Notifier,
        unknown_dir: Path,
        app_in_foreground: Callable[[], bool] | None = None,
    ) -> None:
        self._device = device
        self._vision = vision
        self._notifier = notifier
        self._unknown_dir = unknown_dir
        self._app_in_foreground = app_in_foreground

    def check(self) -> RecoveryOutcome:
        """Quick pass before every state: fix what is fixable in place.

        Returns ``OK`` when the screen shows none of the known-bad markers;
        the task state then has to judge the screen itself.
        """
        # game crash văng về launcher Android: không màn hình nào của game
        # tồn tại nữa — RELOGIN để orchestrator/runner chạy lại login (login
        # tự khởi động app)
        if self._app_in_foreground is not None and not self._app_in_foreground():
            log.warning("recovery: game not in foreground (crashed or closed)")
            return RecoveryOutcome.RELOGIN
        frame = self._vision.capture_screen()
        if self._find_any(DISCONNECT_MARKERS, frame) is not None:
            return self._handle_disconnect(frame)
        revive = self._find_any(REVIVE_BUTTONS, frame)
        if revive is not None:
            return self._tap_and_report("revive", revive)
        retry = self._find_any(RETRY_BUTTONS, frame)
        if retry is not None:
            return self._tap_and_report("config-retry", retry)
        if self._find_any(LOGIN_MARKERS, frame) is not None:
            log.warning("recovery: thrown back to login screen")
            return RecoveryOutcome.RELOGIN
        # màn làm việc hợp lệ (cửa hiệu...) đang mở: đừng dọn popup —
        # X của túi/shop là một phần của màn đó, đóng nó là phá task
        if self._find_any(WORKSPACE_MARKERS, frame) is not None:
            return RecoveryOutcome.OK
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
                if not self._decline_dialog(current):
                    break
            else:
                log.info("recovery: closing popup", at=button.center)
                self._device.tap(*button.center)
            closed += 1
            current = self._vision.capture_screen()
        return RecoveryOutcome.RECOVERED if closed > 0 else RecoveryOutcome.OK

    def _decline_dialog(self, frame: Image) -> bool:
        """Dismiss an NPC story dialog by tapping its BOTTOM-MOST ◆ option.

        Story dialogs block every other tap (quest auto-routing opens them
        uninvited). The bottom option is the game's decline/leave by
        convention; tapping it never accepts anything.
        """
        if DIALOG_BULLET not in self._vision.store:
            return False
        bullets = self._vision.find_all(DIALOG_BULLET, screenshot=frame)
        if not bullets:
            return False
        lowest = max(bullets, key=lambda m: m.bbox[1])
        log.warning("recovery: NPC dialog on screen, declining", at=lowest.center)
        self._device.tap(*lowest.center)
        return True

    def _save_unknown(self, state: str, frame: Image) -> str:
        self._unknown_dir.mkdir(parents=True, exist_ok=True)
        path = self._unknown_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{state}.png"
        cv2.imwrite(str(path), frame)
        return str(path)
