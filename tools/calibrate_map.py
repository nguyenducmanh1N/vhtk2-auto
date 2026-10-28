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
from vhtk.core.vision import Image, TemplateStore, Vision, best_match
from vhtk.logging_setup import setup_logging
from vhtk.mapcal import fit_transform, max_residual
from vhtk.notify import NullNotifier
from vhtk.tasks.navigate import Navigator

log = structlog.get_logger(__name__)

# Dòng danh sách khớp crop đã quét tới mức này thì coi là đúng map.
# So khớp trên MẶT NẠ CHỮ (nhị phân > 200): dòng đang chọn có nền highlight
# còn crop đã quét thì không — match màu thẳng tụt về ~0.31 (đo 27/10),
# match mặt nạ chữ cho 1.00 có mặt / ~0.37 vắng mặt
ROW_MATCH_THRESHOLD = 0.85
ROW_TEXT_BIN_THRESHOLD = 200


def text_mask(img: Image) -> Image:
    """Binary mask of the bright row text (background/highlight removed)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.threshold(gray, ROW_TEXT_BIN_THRESHOLD, 255, cv2.THRESH_BINARY)[1]
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


def select_harvested_row(
    nav: Navigator, vision: Vision, device: Device, out_dir: Path, entry: dict,
    geom, max_pages: int,
) -> None:
    """Scroll the "Tìm map" list until the harvested row crop matches, tap it.

    Xác minh chọn đúng map bằng crop TIÊU ĐỀ đã quét — không cần OCR.
    """
    row_tpl = cv2.imread(str(out_dir / entry["files"]["row"]))
    title_tpl = cv2.imread(str(out_dir / entry["files"]["title"]))
    lx, ly, lw, lh = geom.rect
    # game NHỚ vị trí cuộn của danh sách giữa các lần mở map — kéo về đầu
    # danh sách trước, rồi mới dò xuống. Swipe bắt đầu sát mép trên (y+20)
    # bị game nuốt (đo 27/10) — bắt đầu từ giữa vùng danh sách
    x = lx + lw // 2
    for _ in range(max_pages + 3):
        device.swipe(x, ly + 60, x, ly + 290, duration_ms=500)
        time.sleep(0.6)
    for page in range(max_pages):
        frame = vision.capture_screen()
        score, (bx, by, bw, bh) = best_match(
            text_mask(crop(frame, geom.rect)), text_mask(row_tpl)
        )
        if score >= ROW_MATCH_THRESHOLD:
            device.tap(lx + bx + bw // 2, ly + by + bh // 2)
            time.sleep(1.3)
            check = vision.capture_screen()
            tscore, _ = best_match(crop(check, (300, 30, 360, 34)), title_tpl)
            if tscore < 0.85:
                raise SystemExit(f"LOI: tap dong nhung tieu de khong khop (score {tscore:.2f})")
            log.info("map selected", name=entry["name"], page=page, title_score=round(tscore, 2))
            return
        x = lx + lw // 2
        dy = (geom.visible_rows - 2) * geom.row_pitch
        device.swipe(x, ly + lh - 20, x, ly + lh - 20 - dy, duration_ms=600)
        time.sleep(1.2)
    raise SystemExit(f"LOI: khong tim thay dong {entry['name']!r} sau {max_pages} trang")


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

    out_dir = Path("data/maps") / settings.templates.version
    manifest = load_manifest(out_dir)
    entry = find_entry(manifest, args.map)
    calib_dir = out_dir / f"{entry['index']:03d}_calib"
    calib_dir.mkdir(exist_ok=True)

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
    nav = Navigator(ctx, coords.farm, settings.farm)

    samples: list[dict] = []
    try:
        nav.ensure_standing()
        nav.open_map()
        nav.ensure_find_map_tab()
        select_harvested_row(
            nav, vision, device, out_dir, entry, coords.farm.map_list, args.max_pages
        )
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
            # mở lại màn bản đồ cho điểm kế — map đích vẫn đang được preview
            nav.open_map()
            nav.ensure_find_map_tab()
            select_harvested_row(
                nav, vision, device, out_dir, entry, coords.farm.map_list, args.max_pages
            )
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
    out_dir = Path("data/maps") / settings.templates.version
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
