"""Tests for the device layer's pure logic: InputGate, screencap decoding,
Device shell flow with a fake ADB handle (no real device needed)."""

from __future__ import annotations

import random
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vhtk.config import ActionDelay, Resolution, SafetySettings
from vhtk.core.device import Device, InputGate, decode_screencap
from vhtk.core.errors import ADBError, DeviceLost, KillSwitchTriggered, RateLimited


def make_safety(
    max_per_min: int = 30, min_delay: float = 0.0, max_delay: float = 0.0
) -> SafetySettings:
    return SafetySettings(
        dry_run=True,
        confirm_mode=True,
        confirm_timeout_seconds=300.0,
        kill_switch_file="STOP",
        max_actions_per_minute=max_per_min,
        max_irreversible_per_session=200,
        max_recover_attempts=3,
        action_delay=ActionDelay(min_seconds=min_delay, max_seconds=max_delay),
    )


class FakeClock:
    """Manual clock whose sleep() advances time instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_gate(
    safety: SafetySettings, tmp_path: Path, clock: FakeClock | None = None
) -> tuple[InputGate, FakeClock]:
    clk = clock or FakeClock()
    gate = InputGate(
        safety,
        kill_switch_path=tmp_path / "STOP",
        clock=clk.clock,
        sleep=clk.sleep,
        rng=random.Random(42),
    )
    return gate, clk


# ---------------------------------------------------------------------------
# InputGate
# ---------------------------------------------------------------------------


def test_gate_delay_within_configured_bounds(tmp_path: Path) -> None:
    gate, clk = make_gate(make_safety(min_delay=0.5, max_delay=2.0), tmp_path)
    for _ in range(20):
        gate.wait_turn("tap")
    assert len(clk.sleeps) == 20
    assert all(0.5 <= s <= 2.0 for s in clk.sleeps)


def test_gate_kill_switch_raises_before_acting(tmp_path: Path) -> None:
    gate, _clk = make_gate(make_safety(), tmp_path)
    (tmp_path / "STOP").touch()
    with pytest.raises(KillSwitchTriggered):
        gate.wait_turn("tap")


def test_gate_waits_for_rate_limit_slot_within_grace(tmp_path: Path) -> None:
    gate, clk = make_gate(make_safety(max_per_min=3), tmp_path)
    for _ in range(3):
        gate.wait_turn("tap")
    # slot cũ nhất hết hạn sau 60s; lùi tới 55s → chỉ phải chờ 5s ≤ grace 10s
    clk.now = 55.0
    gate.wait_turn("tap")
    assert 5.0 in clk.sleeps  # đã ngủ đúng phần còn thiếu của cửa sổ


def test_gate_raises_when_wait_exceeds_grace(tmp_path: Path) -> None:
    gate, _clk = make_gate(make_safety(max_per_min=3), tmp_path)
    for _ in range(3):
        gate.wait_turn("tap")
    # cả 3 action vừa xảy ra → action thứ 4 phải chờ ~60s > grace 10s
    with pytest.raises(RateLimited):
        gate.wait_turn("tap")


def test_gate_forgets_actions_outside_window(tmp_path: Path) -> None:
    gate, clk = make_gate(make_safety(max_per_min=2), tmp_path)
    gate.wait_turn("tap")
    gate.wait_turn("tap")
    clk.now += 61.0
    gate.wait_turn("tap")  # không được raise — lịch sử cũ đã hết hạn
    assert clk.sleeps == []  # delay = 0 và không có lần chờ slot nào


# ---------------------------------------------------------------------------
# screencap decoding
# ---------------------------------------------------------------------------


def _raw_frame(width: int, height: int, header_extra: bytes = b"") -> tuple[bytes, np.ndarray]:
    rng = np.random.default_rng(3)
    rgba = rng.integers(0, 256, size=(height, width, 4), dtype=np.uint8)
    header = struct.pack("<III", width, height, 1) + header_extra
    return header + rgba.tobytes(), rgba


@pytest.mark.parametrize("extra", [b"", struct.pack("<I", 0)])  # header 12 và 16 byte
def test_decode_screencap_both_header_sizes(extra: bytes) -> None:
    raw, rgba = _raw_frame(8, 6, extra)
    bgr = decode_screencap(raw)
    assert bgr.shape == (6, 8, 3)
    # kênh B của BGR phải bằng kênh B của RGBA gốc (index 2)
    assert np.array_equal(bgr[:, :, 0], rgba[:, :, 2])
    assert np.array_equal(bgr[:, :, 2], rgba[:, :, 0])


def test_decode_screencap_rejects_bad_format() -> None:
    raw = struct.pack("<III", 4, 4, 99) + b"\x00" * 64
    with pytest.raises(ADBError, match="pixel format"):
        decode_screencap(raw)


def test_decode_screencap_rejects_size_mismatch() -> None:
    raw = struct.pack("<III", 4, 4, 1) + b"\x00" * 10
    with pytest.raises(ADBError, match="size mismatch"):
        decode_screencap(raw)


def test_decode_screencap_rejects_truncated() -> None:
    with pytest.raises(ADBError, match="too short"):
        decode_screencap(b"\x00" * 5)


# ---------------------------------------------------------------------------
# Device with a fake ADB handle
# ---------------------------------------------------------------------------


class FakeAdb:
    """Records shell commands; can fail a given number of times first."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.commands: list[list[str]] = []

    def shell(self, cmd: list[str], **_kwargs: Any) -> str:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("connection reset")
        self.commands.append(list(cmd))
        return ""


class FakeBackend:
    def __init__(self, adb: Any) -> None:
        self.adb = adb
        self.closed = False

    def grab(self) -> Any:
        return np.zeros((540, 960, 3), dtype=np.uint8)

    def close(self) -> None:
        self.closed = True


RESOLUTION = Resolution(width=960, height=540, dpi=160)


def make_device(tmp_path: Path, adb: FakeAdb) -> Device:
    gate, _clk = make_gate(make_safety(), tmp_path)
    return Device(
        "emulator-5554",
        make_safety(),
        RESOLUTION,
        gate=gate,
        adb_factory=lambda _serial: adb,
        backend_factory=FakeBackend,
        sleep=lambda _s: None,
    )


def test_tap_sends_input_command_and_validates_bounds(tmp_path: Path) -> None:
    adb = FakeAdb()
    device = make_device(tmp_path, adb)
    device.tap(480, 270)
    assert adb.commands == [["input", "tap", "480", "270"]]
    with pytest.raises(ValueError, match="outside screen"):
        device.tap(960, 270)  # x == width là ngoài màn hình


def test_long_press_is_in_place_swipe(tmp_path: Path) -> None:
    adb = FakeAdb()
    device = make_device(tmp_path, adb)
    device.long_press(100, 200, duration_ms=700)
    assert adb.commands == [["input", "swipe", "100", "200", "100", "200", "700"]]


def test_reconnect_recovers_from_transient_failures(tmp_path: Path) -> None:
    adb = FakeAdb(fail_times=2)  # 2 lần lỗi đầu, lần retry thứ 2 thành công
    device = make_device(tmp_path, adb)
    device.press_back()
    assert adb.commands == [["input", "keyevent", "4"]]


def test_reconnect_exhausted_raises_device_lost(tmp_path: Path) -> None:
    adb = FakeAdb(fail_times=99)
    device = make_device(tmp_path, adb)
    with pytest.raises(DeviceLost):
        device.press_back()


def test_screenshot_bypasses_gate(tmp_path: Path) -> None:
    adb = FakeAdb()
    gate, clk = make_gate(make_safety(min_delay=1.0, max_delay=1.0), tmp_path)
    device = Device(
        "emulator-5554",
        make_safety(),
        RESOLUTION,
        gate=gate,
        adb_factory=lambda _serial: adb,
        backend_factory=FakeBackend,
        sleep=lambda _s: None,
    )
    device.screenshot()
    assert clk.sleeps == []  # không delay, không tính vào quota


class ScriptedAdb(FakeAdb):
    """FakeAdb whose shell always returns a fixed response."""

    def __init__(self, response: str) -> None:
        super().__init__()
        self.response = response

    def shell(self, cmd: list[str], **_kwargs: Any) -> str:
        self.commands.append(list(cmd))
        return self.response


def test_app_in_foreground_parses_dumpsys(tmp_path: Path) -> None:
    focused = "  mCurrentFocus=Window{a98 u0 com.blackhole.jx2mobile/org.cocos2dx.cpp.AppActivity}"
    device = make_device(tmp_path, ScriptedAdb(focused))

    assert device.app_in_foreground("com.blackhole.jx2mobile") is True
    assert device.app_in_foreground("com.other.app") is False


def test_app_in_foreground_false_on_launcher(tmp_path: Path) -> None:
    launcher = "  mCurrentFocus=Window{b12 u0 com.android.launcher3/.Launcher}"
    device = make_device(tmp_path, ScriptedAdb(launcher))

    assert device.app_in_foreground("com.blackhole.jx2mobile") is False


def test_input_text_sends_command_and_validates_charset(tmp_path: Path) -> None:
    adb = FakeAdb()
    device = make_device(tmp_path, adb)

    device.input_text("user@mail.com")
    assert ["input", "text", "user@mail.com"] in adb.commands

    with pytest.raises(ValueError, match="input_text"):
        device.input_text("has space")
    with pytest.raises(ValueError, match="input_text"):
        device.input_text("tiếngViệt")


def test_press_delete_sends_batch_keyevents(tmp_path: Path) -> None:
    adb = FakeAdb()
    device = make_device(tmp_path, adb)

    device.press_delete(3)
    assert ["input", "keyevent", "67", "67", "67"] in adb.commands

    before = len(adb.commands)
    device.press_delete(0)  # không gửi gì
    assert len(adb.commands) == before
