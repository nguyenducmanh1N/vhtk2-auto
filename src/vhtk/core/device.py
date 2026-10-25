"""ADB device wrapper for one LDPlayer instance.

Design notes:
- Every input action (tap/swipe/key) goes through ``InputGate``: kill-switch
  check, sliding-window rate limit, then a random configurable delay.
  ``screenshot()`` deliberately bypasses the gate — it is a passive read that
  vision polls constantly; counting it against the quota would starve real
  actions.
- Screenshots come from a swappable ``ScreenshotBackend`` (Protocol). The
  default ``AdbScreencapBackend`` uses raw ``screencap`` (RGBA dump, no PNG
  encode) with a PNG fallback; a faster minicap/scrcpy backend can be dropped
  in later without touching any caller.
- Every ADB call is wrapped in reconnect logic: up to 3 retries with
  exponential backoff, then ``DeviceLost``.
"""

from __future__ import annotations

import contextlib
import random
import re
import struct
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast

import cv2
import numpy as np
import numpy.typing as npt
import structlog

from vhtk.config import Resolution, SafetySettings
from vhtk.core.errors import ADBError, DeviceLost, KillSwitchTriggered, RateLimited

log = structlog.get_logger(__name__)

Image: TypeAlias = npt.NDArray[np.uint8]

# Android keycodes — https://developer.android.com/reference/android/view/KeyEvent
KEYCODE_HOME = 3
KEYCODE_BACK = 4
KEYCODE_WAKEUP = 224

_SCREENCAP_FORMAT_RGBA = 1
_MAX_SANE_DIMENSION = 10_000
_EMULATOR_SERIAL_RE = re.compile(r"emulator-(\d+)")
_INPUT_TEXT_SAFE_RE = re.compile(r"[A-Za-z0-9@._\-]+")


# ---------------------------------------------------------------------------
# Input gate: kill-switch + rate limit + random delay
# ---------------------------------------------------------------------------


class InputGate:
    """Mediates every input action according to the safety settings.

    Order per action: kill-switch check → rate limit (sleep-wait for a free
    slot, ``RateLimited`` if the wait would exceed the grace period) → random
    delay in ``[min_seconds, max_seconds]`` → kill-switch re-check (the STOP
    file may have appeared while sleeping).

    Clock/sleep/rng are injectable so the logic is testable without real time.
    """

    RATE_WINDOW_S = 60.0
    RATE_LIMIT_GRACE_S = 10.0

    def __init__(
        self,
        safety: SafetySettings,
        kill_switch_path: Path | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._safety = safety
        self._kill_switch = kill_switch_path or Path(safety.kill_switch_file)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._history: deque[float] = deque()

    def wait_turn(self, action: str) -> None:
        """Block until the given action is allowed to run.

        Raises:
            KillSwitchTriggered: if the kill-switch file exists.
            RateLimited: if waiting for a rate-limit slot would exceed the
                grace period (a bug is spamming actions).
        """
        self._check_kill_switch()
        self._enforce_rate_limit(action)
        delay = self._rng.uniform(
            self._safety.action_delay.min_seconds, self._safety.action_delay.max_seconds
        )
        if delay > 0:
            self._sleep(delay)
        self._check_kill_switch()
        self._history.append(self._clock())

    def _check_kill_switch(self) -> None:
        if self._kill_switch.exists():
            raise KillSwitchTriggered(f"kill-switch file present: {self._kill_switch}")

    def _enforce_rate_limit(self, action: str) -> None:
        limit = self._safety.max_actions_per_minute
        now = self._drop_expired()
        if len(self._history) < limit:
            return
        wait = self._history[0] + self.RATE_WINDOW_S - now
        if wait > self.RATE_LIMIT_GRACE_S:
            raise RateLimited(
                f"action {action!r} would wait {wait:.1f}s for a rate-limit slot "
                f"(limit {limit}/min, grace {self.RATE_LIMIT_GRACE_S}s)"
            )
        log.debug("rate limit reached, waiting", action=action, wait_s=round(wait, 2))
        self._sleep(max(wait, 0.0))
        self._drop_expired()

    def _drop_expired(self) -> float:
        now = self._clock()
        while self._history and now - self._history[0] >= self.RATE_WINDOW_S:
            self._history.popleft()
        return now


# ---------------------------------------------------------------------------
# Screenshot backends
# ---------------------------------------------------------------------------


class ScreenshotBackend(Protocol):
    """Anything that can produce BGR frames for one device."""

    def grab(self) -> Image:
        """Return one fresh BGR screenshot."""
        ...

    def close(self) -> None:
        """Release backend resources (streams, sockets)."""
        ...


def decode_screencap(raw: bytes) -> Image:
    """Decode raw ``screencap`` output (RGBA + 12/16-byte header) to BGR.

    Android < 9 emits a 12-byte header (w, h, format); API 28+ appends a
    4-byte colorspace field. Both are accepted; anything else raises.

    Raises:
        ADBError: on truncated data, unknown pixel format, or size mismatch.
    """
    if len(raw) < 12:
        raise ADBError(f"screencap output too short: {len(raw)} bytes")
    width, height, fmt = struct.unpack_from("<III", raw, 0)
    if fmt != _SCREENCAP_FORMAT_RGBA:
        raise ADBError(f"unsupported screencap pixel format {fmt} (expected RGBA_8888)")
    if not (0 < width < _MAX_SANE_DIMENSION and 0 < height < _MAX_SANE_DIMENSION):
        raise ADBError(f"implausible screencap dimensions {width}x{height}")
    expected = width * height * 4
    for offset in (12, 16):
        if len(raw) - offset == expected:
            arr = np.frombuffer(raw, dtype=np.uint8, offset=offset).reshape(height, width, 4)
            return cast(Image, cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR))
    raise ADBError(f"screencap size mismatch: {len(raw)} bytes for {width}x{height} RGBA")


class AdbScreencapBackend:
    """Default backend: ``adb shell screencap`` raw RGBA dump.

    ~100-250ms per frame at 960x540 — plenty for a 0.5s poll interval, zero
    setup on the device. Falls back to PNG mode when the raw dump cannot be
    decoded (some adbd builds run shell commands under a pty that mangles
    ``\\n`` into ``\\r\\n``).
    """

    def __init__(self, adb_device: Any) -> None:
        self._device = adb_device

    def grab(self) -> Image:
        raw = cast(bytes, self._device.shell(["screencap"], encoding=None))
        try:
            return decode_screencap(raw)
        except ADBError:
            fixed = raw.replace(b"\r\n", b"\n")
            if fixed is not raw:
                with contextlib.suppress(ADBError):
                    return decode_screencap(fixed)
        log.warning("raw screencap decode failed, falling back to PNG mode")
        return self._grab_png()

    def _grab_png(self) -> Image:
        png = cast(bytes, self._device.shell(["screencap", "-p"], encoding=None))
        for data in (png, png.replace(b"\r\n", b"\n")):
            img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                return cast(Image, img)
        raise ADBError("cannot decode screencap output in raw or PNG mode")

    def close(self) -> None:
        pass


BackendFactory: TypeAlias = Callable[[Any], ScreenshotBackend]
AdbFactory: TypeAlias = Callable[[str], Any]


def _default_adb_factory(serial: str) -> Any:
    import adbutils  # import tại chỗ để test không cần adb server

    return adbutils.adb.device(serial=serial)


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------


class Device:
    """One LDPlayer instance controlled over ADB.

    Args:
        serial: ADB serial, e.g. ``emulator-5554``.
        safety: safety section of settings.yaml (rate limit, delays,
            kill-switch file name).
        resolution: expected screen size; input coordinates are validated
            against it to catch bugs before they reach the device.
        gate: input gate override (tests); a fresh one is built by default.
        adb_factory: returns an adbutils device handle for a serial (tests).
        backend_factory: builds the screenshot backend from an adb handle.
        sleep: sleep function used by reconnect backoff (tests).
    """

    RECONNECT_ATTEMPTS = 3

    def __init__(
        self,
        serial: str,
        safety: SafetySettings,
        resolution: Resolution,
        *,
        gate: InputGate | None = None,
        adb_factory: AdbFactory | None = None,
        backend_factory: BackendFactory | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._serial = serial
        self._resolution = resolution
        self._gate = gate or InputGate(safety)
        self._adb_factory = adb_factory or _default_adb_factory
        self._backend_factory: BackendFactory = backend_factory or AdbScreencapBackend
        self._sleep = sleep
        self._adb = self._adb_factory(serial)
        self._backend = self._backend_factory(self._adb)

    @property
    def serial(self) -> str:
        """ADB serial this device is bound to."""
        return self._serial

    def close(self) -> None:
        """Release backend resources."""
        self._backend.close()

    # -- screen ------------------------------------------------------------

    def screenshot(self) -> Image:
        """One fresh BGR frame (bypasses the input gate — passive read)."""
        return cast(Image, self._with_reconnect("screenshot", lambda: self._backend.grab()))

    def is_screen_on(self) -> bool:
        """Whether the display is awake."""
        out = self._shell(["dumpsys", "power"])
        return "mWakefulness=Awake" in out or "state=ON" in out

    def wake(self) -> None:
        """Wake the screen if it is off."""
        if self.is_screen_on():
            return
        self._key(KEYCODE_WAKEUP, action="wake")

    # -- input (all gated) ---------------------------------------------------

    def tap(self, x: int, y: int) -> None:
        """Tap at (x, y). Coordinates are validated against the resolution."""
        self._validate_xy(x, y)
        self._gate.wait_turn("tap")
        self._shell(["input", "tap", str(x), str(y)])
        log.debug("tap", serial=self._serial, x=x, y=y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        """Swipe from (x1, y1) to (x2, y2) over ``duration_ms``."""
        self._validate_xy(x1, y1)
        self._validate_xy(x2, y2)
        self._gate.wait_turn("swipe")
        self._shell(["input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])
        log.debug("swipe", serial=self._serial, frm=(x1, y1), to=(x2, y2), ms=duration_ms)

    def long_press(self, x: int, y: int, duration_ms: int = 600) -> None:
        """Long-press at (x, y) — implemented as an in-place swipe."""
        self._validate_xy(x, y)
        self._gate.wait_turn("long_press")
        self._shell(["input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms)])
        log.debug("long_press", serial=self._serial, x=x, y=y, ms=duration_ms)

    def input_text(self, text: str) -> None:
        """Type text into the currently focused input field (one gated action).

        Only a conservative ASCII charset is accepted — credentials in this
        project are alphanumeric; anything outside the set would need an IME
        and could silently corrupt the field.

        Raises:
            ValueError: if ``text`` contains characters outside ``[A-Za-z0-9@._-]``.
        """
        if not _INPUT_TEXT_SAFE_RE.fullmatch(text):
            raise ValueError("input_text only accepts [A-Za-z0-9@._-]+ (no spaces/unicode)")
        self._gate.wait_turn("input_text")
        self._shell(["input", "text", text])
        # không log nội dung — có thể là mật khẩu
        log.debug("input_text", serial=self._serial, chars=len(text))

    def press_delete(self, count: int) -> None:
        """Press DEL ``count`` times in ONE gated shell call (clear a field)."""
        if count <= 0:
            return
        self._gate.wait_turn("press_delete")
        self._shell(["input", "keyevent", *(["67"] * count)])
        log.debug("press_delete", serial=self._serial, count=count)

    def press_back(self) -> None:
        """Press the BACK key."""
        self._key(KEYCODE_BACK, action="back")

    def press_home(self) -> None:
        """Press the HOME key."""
        self._key(KEYCODE_HOME, action="home")

    # -- app control ---------------------------------------------------------

    def app_is_running(self, package: str) -> bool:
        """Whether a process of ``package`` is alive."""
        return self._shell(["pidof", package]).strip() != ""

    def app_in_foreground(self, package: str) -> bool:
        """Whether ``package`` owns the focused window.

        A game can crash back to the launcher while its process stays alive,
        so "running" is NOT enough to assume it is on screen.
        """
        out = self._shell(["dumpsys", "window"])
        return any(
            package in line and ("mCurrentFocus" in line or "mFocusedApp" in line)
            for line in out.splitlines()
        )

    def start_app(self, package: str) -> None:
        """Launch an app via monkey (no activity name needed)."""
        self._shell(["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])
        log.info("app started", serial=self._serial, package=package)

    def stop_app(self, package: str) -> None:
        """Force-stop an app."""
        self._shell(["am", "force-stop", package])
        log.info("app stopped", serial=self._serial, package=package)

    # -- internals -----------------------------------------------------------

    def _key(self, keycode: int, action: str) -> None:
        self._gate.wait_turn(action)
        self._shell(["input", "keyevent", str(keycode)])
        log.debug("keyevent", serial=self._serial, keycode=keycode)

    def _validate_xy(self, x: int, y: int) -> None:
        if not (0 <= x < self._resolution.width and 0 <= y < self._resolution.height):
            raise ValueError(
                f"coordinates ({x}, {y}) outside screen "
                f"{self._resolution.width}x{self._resolution.height}"
            )

    def _shell(self, cmd: list[str]) -> str:
        return cast(str, self._with_reconnect("shell", lambda: self._adb.shell(cmd)))

    def _with_reconnect(self, op: str, fn: Callable[[], Any]) -> Any:
        """Run an ADB operation, reconnecting on connection errors.

        Raises:
            DeviceLost: after ``RECONNECT_ATTEMPTS`` failed reconnects.
        """
        import adbutils

        last_exc: Exception | None = None
        for attempt in range(self.RECONNECT_ATTEMPTS + 1):
            try:
                return fn()
            except (adbutils.AdbError, OSError) as exc:
                last_exc = exc
                if attempt == self.RECONNECT_ATTEMPTS:
                    break
                log.warning(
                    "adb operation failed, reconnecting",
                    serial=self._serial,
                    op=op,
                    attempt=attempt + 1,
                    error=str(exc),
                )
                self._reconnect(attempt)
        raise DeviceLost(
            f"device {self._serial} lost during {op!r} "
            f"after {self.RECONNECT_ATTEMPTS} reconnect attempts: {last_exc}"
        ) from last_exc

    def _reconnect(self, attempt: int) -> None:
        self._sleep(float(2**attempt))  # backoff 1s / 2s / 4s
        # emulator-<port>: adbd nghe TCP ở cổng <port>+1 — thử connect lại
        # phòng khi adb server đã đánh rơi thiết bị
        m = _EMULATOR_SERIAL_RE.fullmatch(self._serial)
        if m is not None:
            with contextlib.suppress(Exception):
                import adbutils

                adbutils.adb.connect(f"127.0.0.1:{int(m.group(1)) + 1}", timeout=3.0)
        with contextlib.suppress(Exception):
            self._backend.close()
        self._adb = self._adb_factory(self._serial)
        self._backend = self._backend_factory(self._adb)
