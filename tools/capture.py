"""CLI tool: capture a screenshot from an LDPlayer instance and cut templates.

Examples:
    # Chi chup man hinh day du
    python tools/capture.py --serial emulator-5554

    # Cat template bang cua so keo chon chuot (OpenCV window)
    python tools/capture.py --serial emulator-5554 --name common/btn_confirm

    # Cat template theo toa do cho truoc
    python tools/capture.py --serial emulator-5554 --name common/btn_confirm \
        --rect 850 20 60 40
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

# Cho phép chạy trực tiếp `python tools/capture.py` khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhtk.config import Settings, load_settings
from vhtk.core.vision import Image, best_match


def capture_bgr(serial: str) -> Image:
    """Capture one screenshot from an ADB device as a BGR numpy image."""
    import adbutils  # import tại chỗ để tool vẫn chạy phần offline khi thiếu adbutils

    device = adbutils.adb.device(serial=serial)
    pil_img = device.screenshot().convert("RGB")
    frame: Image = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    return frame


def _save_full(frame: Image, settings: Settings, serial: str) -> Path:
    out_dir = settings.logging.dir / "captures"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{stamp}_{serial}.png"
    cv2.imwrite(str(path), frame)
    return path


def _select_rect(frame: Image) -> tuple[int, int, int, int]:
    print("Keo chuot chon vung template, nhan ENTER/SPACE de xac nhan, c de huy.")
    x, y, w, h = (int(v) for v in cv2.selectROI("capture", frame, showCrosshair=True))
    cv2.destroyAllWindows()
    return x, y, w, h


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture screenshots and cut templates")
    parser.add_argument("--serial", required=True, help="ADB serial, e.g. emulator-5554")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--name", help='template name to save, e.g. "common/btn_confirm"')
    parser.add_argument(
        "--rect",
        nargs=4,
        type=int,
        metavar=("X", "Y", "W", "H"),
        help="crop rectangle; omit to select with the mouse",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace existing template")
    args = parser.parse_args()

    settings = load_settings(Path(args.settings))
    frame = capture_bgr(args.serial)
    full_path = _save_full(frame, settings, args.serial)
    print(f"full screenshot saved: {full_path} ({frame.shape[1]}x{frame.shape[0]})")
    if args.name is None:
        return 0

    x, y, w, h = args.rect if args.rect else _select_rect(frame)
    if w <= 0 or h <= 0:
        print("empty selection, nothing saved")
        return 1
    crop = frame[y : y + h, x : x + w].copy()

    out_path = settings.templates.root / f"{args.name}.png"
    if out_path.exists() and not args.overwrite:
        print(f"refusing to overwrite {out_path} (use --overwrite)")
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), crop)
    print(f"template saved: {out_path} ({w}x{h} at {x},{y})")

    # Match thử ngay trên ảnh nguồn để phát hiện template quá "trơn" (ít đặc trưng)
    score, bbox = best_match(
        cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    )
    threshold = settings.templates.match_threshold
    verdict = "OK" if score >= threshold else "LOW"
    print(f"self-match: score={score:.3f} threshold={threshold} at {bbox} [{verdict}]")
    print(
        "note: score tren chinh anh nguon luon cao — chay tools/template_check.py "
        "tren man hinh moi de kiem tra that."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
