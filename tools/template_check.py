"""CLI tool: match every stored template against the current screen.

Dung de kiem tra nhanh sau khi game update: template nao con khop, template
nao vo va can chup lai (tao version template moi, khong sua de).

Usage:
    python tools/template_check.py --serial emulator-5554
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

# Cho phép chạy trực tiếp `python tools/template_check.py` khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhtk.config import load_settings
from vhtk.core.vision import Image, TemplateStore, Vision, VisionError


def capture_bgr(serial: str) -> Image:
    """Capture one screenshot from an ADB device as a BGR numpy image."""
    import adbutils

    device = adbutils.adb.device(serial=serial)
    pil_img = device.screenshot().convert("RGB")
    frame: Image = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="Check all templates against current screen")
    parser.add_argument("--serial", required=True, help="ADB serial, e.g. emulator-5554")
    parser.add_argument("--settings", default="config/settings.yaml")
    args = parser.parse_args()

    settings = load_settings(Path(args.settings))
    store = TemplateStore(settings.templates.root)
    if not store.names:
        print(f"no templates found under {settings.templates.root}")
        return 0

    frame = capture_bgr(args.serial)
    vision = Vision(
        store,
        capture=lambda: frame,
        cfg=settings.vision,
        default_threshold=settings.templates.match_threshold,
    )

    misses = 0
    for name in store.names:
        threshold = vision.threshold_for(name)
        try:
            m = vision.locate(name, screenshot=frame)
        except VisionError as exc:
            misses += 1
            print(f"MISS  {name:<45} error: {exc}")
            continue
        ok = m.score >= threshold
        misses += 0 if ok else 1
        status = "OK  " if ok else "MISS"
        print(f"{status}  {name:<45} score={m.score:.3f} threshold={threshold:.2f}")

    total = len(store.names)
    print(f"\n{total - misses}/{total} templates matched")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main())
