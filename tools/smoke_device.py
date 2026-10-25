"""Smoke test for the device-control layer (run on the Windows machine).

Steps, each timed: list instances, connect to one, take 3 screenshots (saving
the last to logs/smoke_screenshot.png), tap the screen center through the real
input gate, then report screen/app state.

Examples:
    # Tu dong: liet ke instance, chon instance dang chay dau tien
    python tools/smoke_device.py

    # Chon instance cu the theo index, hoac noi thang qua serial (bo qua ldconsole)
    python tools/smoke_device.py --index 1
    python tools/smoke_device.py --serial emulator-5554
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import cv2

# Cho phép chạy trực tiếp `python tools/smoke_device.py` khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhtk.config import Settings, load_settings
from vhtk.core.device import Device
from vhtk.core.errors import VhtkError
from vhtk.core.ldplayer import Instance, LDPlayerManager

T = TypeVar("T")


def timed(label: str, fn: Callable[[], T]) -> T:
    """Run ``fn``, print its wall time, return its result."""
    t0 = time.perf_counter()
    result = fn()
    dt_ms = (time.perf_counter() - t0) * 1000
    print(f"  {label:<42} {dt_ms:8.1f} ms")
    return result


def pick_instance(instances: list[Instance], index: int | None) -> Instance:
    """Choose the requested (or first running) instance with a usable serial."""
    if index is not None:
        matches = [i for i in instances if i.index == index]
        if not matches:
            raise SystemExit(f"no instance with index {index}")
        inst = matches[0]
        if not inst.running or inst.adb_serial is None:
            raise SystemExit(
                f"instance {index} ({inst.name!r}) is not running or has no "
                "resolvable serial — start it in LDPlayer first"
            )
        return inst
    for inst in instances:
        if inst.running and inst.adb_serial is not None:
            return inst
    raise SystemExit("no running instance with a resolvable serial — start one first")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the device layer")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--index", type=int, help="LDPlayer instance index to use")
    parser.add_argument("--serial", help="connect directly to this ADB serial (skip ldconsole)")
    args = parser.parse_args()

    settings: Settings = load_settings(Path(args.settings))

    serial = args.serial
    if serial is None:
        mgr = LDPlayerManager(settings.ldplayer.ldconsole_path)
        print("== instances ==")
        instances = timed("list_instances", mgr.list_instances)
        for inst in instances:
            state = "RUNNING" if inst.running else "stopped"
            print(f"  [{inst.index}] {inst.name:<24} {state:<8} serial={inst.adb_serial}")
        chosen = pick_instance(instances, args.index)
        serial = chosen.adb_serial
        print(f"\nusing instance [{chosen.index}] {chosen.name!r} -> {serial}")

    print(f"\n== device {serial} ==")
    device = timed("connect", lambda: Device(serial, settings.safety, settings.ldplayer.resolution))
    try:
        frame = None
        for i in range(3):
            frame = timed(f"screenshot #{i + 1}", device.screenshot)
        assert frame is not None
        h, w = frame.shape[:2]
        out_path = settings.logging.dir / "smoke_screenshot.png"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), frame)
        print(f"  last frame {w}x{h} saved -> {out_path}")
        if (w, h) != (settings.ldplayer.resolution.width, settings.ldplayer.resolution.height):
            print(
                f"  WARNING: frame size {w}x{h} != configured "
                f"{settings.ldplayer.resolution.width}x{settings.ldplayer.resolution.height}"
                " — templates will not match; fix the instance resolution"
            )

        timed(
            "tap center (through input gate)",
            lambda: device.tap(
                settings.ldplayer.resolution.width // 2, settings.ldplayer.resolution.height // 2
            ),
        )
        screen_on = timed("is_screen_on", device.is_screen_on)
        print(f"  screen on: {screen_on}")
        game_running = timed(
            "app_is_running(game)", lambda: device.app_is_running(settings.game.package_name)
        )
        print(f"  game running ({settings.game.package_name}): {game_running}")
    finally:
        device.close()

    print("\nsmoke test finished OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except VhtkError as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
