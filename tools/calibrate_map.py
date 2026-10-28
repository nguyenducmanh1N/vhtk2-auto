"""Collect pixel<->game-coordinate calibration samples for one map.

Với map đích đã có trong ``data/maps/<version>/manifest.yaml``: mở màn bản
đồ, chọn đúng map (khớp crop dòng tên đã quét), tap lần lượt các điểm trên
lưới qua ảnh preview. Điểm hợp lệ làm map đóng và game in "Đi: <Map>(x, y)"
ở đáy màn — tool chụp dòng đó lại rồi mở map tap tiếp. Kết quả:

    data/maps/<version>/<idx>_calib/point_<k>.png   — crop dòng "Đi:"
    data/maps/<version>/<idx>_calib/samples.yaml    — pixel từng điểm,
        trường ``game`` để trống — người đọc ảnh điền (OCR tự động khi có
        PaddleOCR trên Windows), rồi chạy fit:

    python tools/calibrate_map.py --map "Tuyền Châu" --fit

``--fit`` đọc samples.yaml (đã điền game), fit MapTransform, kiểm sai số và
ghi ``calibration`` vào manifest.yaml của kho map.

Chạy khi runner farm ĐANG DỪNG. Nhân vật sẽ ĐI THẬT vài bước giữa các điểm
(điểm sau ghi đè đích của điểm trước) — kết thúc tool nhân vật có thể đang
dở đường tới map đích; farm task sau đó tự định tuyến lại từ bất kỳ đâu.

Example:
    python tools/calibrate_map.py --map "Tuyền Châu" --max-success 4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Cho phép chạy trực tiếp khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cv2
import structlog
import yaml

from vhtk.config import Settings, load_account, load_coordinates, load_settings
from vhtk.core.device import Device
from vhtk.core.errors import VerifyFailed
from vhtk.core.fsm import TaskContext
from vhtk.core.recovery import Recovery
from vhtk.core.safety import Safety
from vhtk.core.vision import Image, TemplateStore, Vision
from vhtk.logging_setup import setup_logging
from vhtk.mapcal import fit_transform, max_residual
from vhtk.mapstore import MapStore, maps_dir
from vhtk.notify import NullNotifier
from vhtk.tasks.navigate import Navigator

log = structlog.get_logger(__name__)

# Chờ dòng "Đi:" hiện sau khi map đóng
TRAVEL_TEXT_SETTLE = 0.7


def crop(frame: Image, rect: tuple[int, int, int, int]) -> Image:
    x, y, w, h = rect
    return frame[y : y + h, x : x + w]


def load_manifest(out_dir: Path) -> dict:
    return yaml.safe_load((out_dir / "manifest.yaml").read_text(encoding="utf-8"))


def find_entry(manifest: dict, name: str) -> dict:
    for e in manifest["maps"]:
        if e["name"] == name:
            return e
    raise SystemExit(f"LOI: map {name!r} khong co trong manifest (chay harvest_maps truoc)")


def grid_points(rect: tuple[int, int, int, int], cols: int, rows: int) -> list[tuple[int, int]]:
    """Candidate tap points: an inner grid over the map image area."""
    x, y, w, h = rect
    return [
        (x + int(w * (i + 1) / (cols + 1)), y + int(h * (j + 1) / (rows + 1)))
        for j in range(rows)
        for i in range(cols)
    ]


def collect(args: argparse.Namespace) -> int:
    settings: Settings = load_settings(Path(args.settings))
    setup_logging(settings.logging.dir, settings.logging.level)
    account = load_account(Path(args.account))
    coords = load_coordinates(settings.paths.coordinates_file)
    if coords.farm.map_list is None or coords.farm.travel_text_rect is None:
        print("LOI: thieu farm.map_list / farm.travel_text_rect trong coordinates.yaml")
        return 1

    out_dir = maps_dir(settings.templates.version)
    manifest = load_manifest(out_dir)
    entry = find_entry(manifest, args.map)
    calib_dir = out_dir / f"{entry['index']:03d}_calib"
    calib_dir.mkdir(exist_ok=True)
    map_store = MapStore(out_dir)
    store_entry = map_store.get(args.map)
    if store_entry is None:
        raise SystemExit(f"LOI: map {args.map!r} chua co ten/du file trong kho")

    store = TemplateStore(settings.templates.root)
    device = Device(account.ldplayer.adb_serial, settings.safety, settings.ldplayer.resolution)
    vision = Vision(store, device.screenshot, settings.vision, settings.templates.match_threshold)
    notifier = NullNotifier()
    safety = Safety(settings.safety, notifier, settings.logging.dir / "dry_run")
    recovery = Recovery(
        device, vision, notifier, settings.logging.dir / "unknown",
        app_in_foreground=lambda: device.app_in_foreground(settings.game.package_name),
    )
    ctx = TaskContext(
        device=device, vision=vision, safety=safety, recovery=recovery,
        notifier=notifier, settings=settings, account=account,
        log_dir=settings.logging.dir,
    )
    farm_cfg = settings.farm.model_copy(update={"map_list_max_pages": args.max_pages})
    nav = Navigator(ctx, coords.farm, farm_cfg, store=map_store)

    samples: list[dict] = []
    try:
        nav.ensure_standing()
        nav.open_map()
        nav.select_map_from_store(store_entry)
        for px, py in grid_points(coords.farm.map_image_rect, args.grid_cols, args.grid_rows):
            if len(samples) >= args.max_success:
                break
            safety.check_kill_switch()
            try:
                nav.tap_map_point(px, py)
            except VerifyFailed:
                log.info("point not walkable, skipping", px=px, py=py)
                continue
            time.sleep(TRAVEL_TEXT_SETTLE)
            frame = vision.capture_screen()
            fname = f"point_{len(samples)}.png"
            cv2.imwrite(str(calib_dir / fname), crop(frame, coords.farm.travel_text_rect))
            samples.append({"pixel": [px, py], "crop": fname, "game": None})
            log.info("sample captured", pixel=[px, py], n=len(samples))
            # mở lại màn bản đồ cho điểm kế — tiêu đề reset về map hiện tại
            # nên phải chọn lại map đích từ kho
            nav.open_map()
            nav.select_map_from_store(store_entry)
        nav.close_map()
    finally:
        device.close()

    (calib_dir / "samples.yaml").write_text(
        yaml.safe_dump({"map": entry["name"], "samples": samples}, allow_unicode=True,
                       sort_keys=False),
        encoding="utf-8",
    )
    print(f"== {len(samples)} samples -> {calib_dir}/samples.yaml ==")
    print("dien toa do 'game: [x, y]' tu cac anh point_*.png roi chay --fit")
    return 0 if len(samples) >= 2 else 1


def fit(args: argparse.Namespace) -> int:
    settings: Settings = load_settings(Path(args.settings))
    out_dir = maps_dir(settings.templates.version)
    manifest = load_manifest(out_dir)
    entry = find_entry(manifest, args.map)
    calib_dir = out_dir / f"{entry['index']:03d}_calib"
    data = yaml.safe_load((calib_dir / "samples.yaml").read_text(encoding="utf-8"))
    pairs = [
        ((s["pixel"][0], s["pixel"][1]), (s["game"][0], s["game"][1]))
        for s in data["samples"]
        if s.get("game")
    ]
    if len(pairs) < 2:
        print(f"LOI: moi co {len(pairs)} cap da dien toa do, can >= 2")
        return 1
    t = fit_transform(pairs)
    err = max_residual(t, pairs)
    print(f"fit: sx={t.sx:.4f} ox={t.ox:.1f} sy={t.sy:.4f} oy={t.oy:.1f} | max residual {err:.1f}")
    if err > 3.0:
        print("LOI: sai so > 3 don vi toa do — kiem tra lai cac cap da nhap, KHONG luu")
        return 1
    entry["calibration"] = {
        "sx": round(t.sx, 6), "ox": round(t.ox, 3),
        "sy": round(t.sy, 6), "oy": round(t.oy, 3),
        "pairs": len(pairs), "max_residual": round(err, 2),
    }
    (out_dir / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(f"== calibration saved for {entry['name']!r} ==")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate pixel<->game coords for one map")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--account", default="config/accounts/dev.yaml")
    parser.add_argument("--map", required=True, help="ten map dung nhu manifest")
    parser.add_argument("--max-success", type=int, default=4)
    parser.add_argument("--grid-cols", type=int, default=5)
    parser.add_argument("--grid-rows", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--fit", action="store_true", help="fit tu samples.yaml da dien")
    args = parser.parse_args()
    return fit(args) if args.fit else collect(args)


if __name__ == "__main__":
    sys.exit(main())
