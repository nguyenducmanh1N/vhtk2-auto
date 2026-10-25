"""CLI tool: cut templates offline from a saved full-frame screenshot.

Companion to capture.py for transient screens (login flow, popups) where
re-capturing live per crop is impractical: save the full frame once, then cut
many templates from it. Each crop is matched back against the source frame
(grayscale, like Vision's default) and the score is printed; a low score means
the crop is not distinctive enough.

Example:
    python tools/crop_template.py --frame flow/title.png \
        --cut login/btn_enter 400 357 160 36 \
        --cut login/title_logo 330 115 300 70
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

# Cho phép chạy trực tiếp khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhtk.config import Settings, load_settings
from vhtk.core.vision import Image, best_match


def cut_one(
    frame: Image, root: Path, name: str, x: int, y: int, w: int, h: int, overwrite: bool
) -> bool:
    """Cut one template, save it under ``root`` and verify it matches back."""
    ih, iw = frame.shape[:2]
    if not (x >= 0 and y >= 0 and x + w <= iw and y + h <= ih and w > 0 and h > 0):
        print(f"  {name}: rect ({x},{y},{w},{h}) outside frame {iw}x{ih} -- SKIP")
        return False
    out = (root / name).with_suffix(".png")
    if out.exists() and not overwrite:
        print(f"  {name}: already exists (use --overwrite) -- SKIP")
        return False
    crop = frame[y : y + h, x : x + w]
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), crop)
    score, bbox = best_match(
        cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    )
    ok = score >= 0.9 and abs(bbox[0] - x) <= 2 and abs(bbox[1] - y) <= 2
    verdict = "OK" if ok else "CHECK!"
    print(f"  {name}: saved {w}x{h} -> {out} | match {score:.3f} at {bbox[:2]} {verdict}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Cut templates from a saved screenshot")
    parser.add_argument("--frame", required=True, help="path to the full-frame PNG")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--cut",
        nargs=5,
        action="append",
        metavar=("NAME", "X", "Y", "W", "H"),
        required=True,
        help="template name + crop rect; repeatable",
    )
    args = parser.parse_args()

    settings: Settings = load_settings(Path(args.settings))
    frame = cv2.imread(args.frame, cv2.IMREAD_COLOR)
    if frame is None:
        print(f"cannot read frame: {args.frame}")
        return 1
    root = settings.templates.root
    print(f"frame {frame.shape[1]}x{frame.shape[0]} -> templates under {root}")
    all_ok = True
    for name, x, y, w, h in args.cut:
        all_ok &= cut_one(frame, root, name, int(x), int(y), int(w), int(h), args.overwrite)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
