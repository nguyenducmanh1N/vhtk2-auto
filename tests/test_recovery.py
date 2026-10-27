"""Tests for pre-state recovery: popups, revive, disconnect, relogin, unknown.

Uses a real TemplateStore/Vision over synthetic images: small random patches
act as templates and are pasted onto a gray canvas at known positions.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from vhtk.config import VisionSettings
from vhtk.core.recovery import Recovery, RecoveryOutcome
from vhtk.core.vision import Image, TemplateStore, Vision

PATCH = 20
CANVAS = (300, 400)  # (h, w)


def make_patch(seed: int) -> Image:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, (PATCH, PATCH, 3), dtype=np.uint8)  # type: ignore[return-value]


# Mỗi template một patch ngẫu nhiên riêng — match điểm ~1.0 đúng chỗ dán
PATCHES: dict[str, Image] = {
    "common/btn_close": make_patch(1),
    "common/btn_revive": make_patch(2),
    "common/disconnected": make_patch(3),
    "common/btn_reconnect": make_patch(4),
    "login/title": make_patch(5),
    "common/dialog_bullet": make_patch(6),
}


def write_store(root: Path, names: list[str]) -> TemplateStore:
    for name in names:
        path = (root / name).with_suffix(".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), PATCHES[name])
    return TemplateStore(root)


def canvas_with(*placements: tuple[str, int, int]) -> Image:
    img = np.full((*CANVAS, 3), 90, dtype=np.uint8)
    for name, x, y in placements:
        img[y : y + PATCH, x : x + PATCH] = PATCHES[name]
    return img  # type: ignore[return-value]


class ScriptedCapture:
    """Returns frames from a list; the last frame repeats forever."""

    def __init__(self, frames: list[Image]) -> None:
        self.frames = list(frames)

    def __call__(self) -> Image:
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]


class FakeDevice:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class StubNotifier:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.photos: list[str] = []

    def send_text(self, text: str) -> None:
        self.texts.append(text)

    def send_photo(self, image: Image, caption: str) -> None:
        self.photos.append(caption)

    def wait_confirm(self, prompt: str, image: Image | None, timeout: float) -> bool:
        return False


def vision_cfg() -> VisionSettings:
    return VisionSettings(
        default_timeout=0.2,
        poll_interval=0.01,
        grayscale_match=True,
        nms_overlap_threshold=0.3,
    )


def make_recovery(
    tmp_path: Path,
    frames: list[Image],
    template_names: list[str] | None = None,
) -> tuple[Recovery, FakeDevice, StubNotifier]:
    store = write_store(tmp_path / "templates", template_names or list(PATCHES))
    vision = Vision(store, ScriptedCapture(frames), vision_cfg(), default_threshold=0.9)
    device = FakeDevice()
    notifier = StubNotifier()
    recovery = Recovery(device, vision, notifier, unknown_dir=tmp_path / "unknown")
    return recovery, device, notifier


def clean_frame() -> Image:
    return canvas_with()


def test_clean_screen_is_ok(tmp_path: Path) -> None:
    recovery, device, _ = make_recovery(tmp_path, [clean_frame()])

    assert recovery.check() is RecoveryOutcome.OK
    assert device.taps == []


def test_popup_is_closed(tmp_path: Path) -> None:
    popup = canvas_with(("common/btn_close", 350, 30))
    recovery, device, _ = make_recovery(tmp_path, [popup, clean_frame()])

    assert recovery.check() is RecoveryOutcome.RECOVERED
    # tap đúng tâm nút X: (350+10, 30+10)
    assert device.taps == [(360, 40)]


def test_stacked_popups_closed_up_to_cap(tmp_path: Path) -> None:
    popup = canvas_with(("common/btn_close", 350, 30))
    # popup "lì": không bao giờ biến mất — chỉ được bấm tối đa 3 lần rồi thoát
    recovery, device, _ = make_recovery(tmp_path, [popup])

    assert recovery.check() is RecoveryOutcome.RECOVERED
    assert len(device.taps) == 3


def test_revive_button_is_tapped(tmp_path: Path) -> None:
    dead = canvas_with(("common/btn_revive", 180, 200))
    recovery, device, _ = make_recovery(tmp_path, [dead, clean_frame()])

    assert recovery.check() is RecoveryOutcome.RECOVERED
    assert device.taps == [(190, 210)]


def test_login_screen_requests_relogin(tmp_path: Path) -> None:
    login = canvas_with(("login/title", 100, 50))
    recovery, device, _ = make_recovery(tmp_path, [login])

    assert recovery.check() is RecoveryOutcome.RELOGIN
    assert device.taps == []


def test_disconnect_taps_reconnect(tmp_path: Path) -> None:
    dc = canvas_with(("common/disconnected", 100, 100), ("common/btn_reconnect", 200, 160))
    recovery, device, _ = make_recovery(tmp_path, [dc, clean_frame()])

    assert recovery.check() is RecoveryOutcome.RECOVERED
    assert device.taps == [(210, 170)]


def test_disconnect_beats_generic_popup(tmp_path: Path) -> None:
    # Màn mất kết nối cũng có nút X — phải xử lý theo nhánh disconnect,
    # không được đóng mù như popup thường
    dc = canvas_with(("common/disconnected", 100, 100), ("common/btn_close", 350, 30))
    recovery, device, _ = make_recovery(tmp_path, [dc, clean_frame()])

    assert recovery.check() is RecoveryOutcome.RECOVERED
    # không có nút reconnect trên màn → không tap gì, chờ game tự xử lý
    assert device.taps == []


def test_unknown_screen_saves_evidence_and_notifies(tmp_path: Path) -> None:
    recovery, _device, notifier = make_recovery(tmp_path, [clean_frame()])

    outcome = recovery.on_state_failure("OPEN_BAG", "StateTimeout: bag button not found")

    assert outcome is RecoveryOutcome.UNKNOWN
    saved = list((tmp_path / "unknown").glob("*.png"))
    assert len(saved) == 1 and "OPEN_BAG" in saved[0].name
    assert len(notifier.photos) == 1 and "OPEN_BAG" in notifier.photos[0]


def test_state_failure_with_fixable_screen_recovers(tmp_path: Path) -> None:
    popup = canvas_with(("common/btn_close", 350, 30))
    recovery, _, notifier = make_recovery(tmp_path, [popup, clean_frame()])

    assert recovery.on_state_failure("FARM", "timeout") is RecoveryOutcome.RECOVERED
    assert not (tmp_path / "unknown").exists()
    assert notifier.photos == []


def test_partial_template_store_is_tolerated(tmp_path: Path) -> None:
    # Store chỉ có nút X — các marker khác chưa chụp cũng không được nổ KeyError
    recovery, device, _ = make_recovery(
        tmp_path, [clean_frame()], template_names=["common/btn_close"]
    )

    assert recovery.check() is RecoveryOutcome.OK
    assert device.taps == []


def test_npc_dialog_declined_via_bottom_option(tmp_path: Path) -> None:
    """Hộp thoại NPC cốt truyện: 2 lựa chọn ◆ — recovery phải tap lựa chọn
    DƯỚI CÙNG (quy ước của game: từ chối/rời đi), không tap lựa chọn trên."""
    dialog = canvas_with(
        ("common/dialog_bullet", 60, 100),  # lựa chọn trên (có thể là NHẬN quest)
        ("common/dialog_bullet", 60, 200),  # lựa chọn dưới = từ chối
    )
    recovery, device, _ = make_recovery(tmp_path, [dialog, clean_frame()])

    outcome = recovery.check()

    assert outcome is RecoveryOutcome.RECOVERED
    assert len(device.taps) == 1
    assert device.taps[0][1] > 200  # tâm patch dưới (y=200..220)
