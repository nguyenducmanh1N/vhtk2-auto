"""Harvest the game's map list for navigation data (map picker in the GUI).

Taps each row of the map screen's "Tìm map" list, waits for the preview to
render, and crops per-map data. Under ``data/maps/<template_version>/``:

    NNN_map.png    — ảnh bản đồ (vùng map_image_rect) cho GUI chọn điểm farm
    NNN_row.png    — crop dòng tên trong danh sách (nguồn làm template
                     map/row_<slug> khi cần farm map đó)
    NNN_title.png  — crop thanh tiêu đề (người vận hành đọc tên map)
    manifest.yaml  — entry từng map; trường ``name`` để trống, người vận hành
                     (hoặc OCR trên Windows) điền sau

Chạy khi runner farm ĐANG DỪNG (./vhtk stop) — hai bên giành nhau màn hình.
Nhân vật phải đã đăng nhập; tool tự lo đứng dậy/mở màn bản đồ.

Example:
    python tools/harvest_maps.py --max-pages 8
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

from vhtk.config import (
    MapListGeometry,
    Settings,
    load_account,
    load_coordinates,
    load_settings,
)
from vhtk.core.device import Device
from vhtk.core.fsm import TaskContext
from vhtk.core.recovery import Recovery
from vhtk.core.safety import Safety
from vhtk.core.vision import Image, TemplateStore, Vision
from vhtk.logging_setup import setup_logging
from vhtk.notify import NullNotifier
from vhtk.tasks.navigate import (
    TPL_TAB_FIND_MAP_OFF,
    TPL_TAB_FIND_MAP_ON,
    Navigator,
)

log = structlog.get_logger(__name__)

# Dòng crop giống nhau tới mức này (match TM_CCOEFF_NORMED) thì coi là map đã
# gặp — danh sách cuộn theo trang không khít nên dòng cũ sẽ xuất hiện lại.
# 0.92 từng nuốt nhầm "Đại Thảo Nguyên 2" vì chỉ khác "...Nguyên 1" một chữ
# số (27/10) — nâng lên 0.97; bản trùng thật sự giống hệt (score ~0.99+)
ROW_DUP_THRESHOLD = 0.97
# Chờ preview map render xong sau khi tap dòng (animation + tải ảnh)
PREVIEW_SETTLE_SECONDS = 1.3


def crop(frame: Image, rect: tuple[int, int, int, int]) -> Image:
    x, y, w, h = rect
    return frame[y : y + h, x : x + w]


def row_rect(geom: MapListGeometry, index: int) -> tuple[int, int, int, int]:
    """Crop rect of one visible list row (x, y, w, h)."""
    lx, _, lw, _ = geom.rect
    y = geom.first_row_y + index * geom.row_pitch - geom.row_pitch // 2 + 2
    return (lx + 4, y, lw - 8, geom.row_pitch - 4)


def is_known_row(row: Image, known: list[Image]) -> bool:
    """Does this (highlighted) row crop match one already harvested?"""
    gray = cv2.cvtColor(row, cv2.COLOR_BGR2GRAY)
    for k in known:
        kg = cv2.cvtColor(k, cv2.COLOR_BGR2GRAY)
        if kg.shape != gray.shape:
            continue
        score = float(cv2.matchTemplate(gray, kg, cv2.TM_CCOEFF_NORMED)[0, 0])
        if score >= ROW_DUP_THRESHOLD:
            return True
    return False


def row_is_empty(row: Image) -> bool:
    """A list row with no text (end of the list on the last page)."""
    gray = cv2.cvtColor(row, cv2.COLOR_BGR2GRAY)
    # chữ sáng trên nền panel tối — không có pixel sáng nghĩa là dòng trống
    return int((gray > 140).sum()) < 30


def ensure_find_map_tab(nav: Navigator, vision: Vision, device: Device) -> None:
    """Switch the map screen's left panel to the "Tìm map" tab."""
    hit = vision.wait_any([TPL_TAB_FIND_MAP_ON, TPL_TAB_FIND_MAP_OFF], timeout=8.0)
    if hit is None:
        raise RuntimeError("map tabs not visible — is the map screen open?")
    if hit[0] == TPL_TAB_FIND_MAP_OFF:
        device.tap(*hit[1].center)
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Harvest the map list into data/maps/")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument(
        "--account", default="config/accounts/dev.yaml",
        help="account YAML (lay adb_serial cua thiet bi)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=8,
        help="scroll at most N list pages (safety bound)",
    )
    args = parser.parse_args()

    settings: Settings = load_settings(Path(args.settings))
    account = load_account(Path(args.account))
    setup_logging(settings.logging.dir, settings.logging.level)
    coords = load_coordinates(settings.paths.coordinates_file)
    geom = coords.farm.map_list
    if geom is None:
        print("LOI: farm.map_list chua co trong coordinates.yaml")
        return 1

    out_dir = Path("data/maps") / settings.templates.version
    out_dir.mkdir(parents=True, exist_ok=True)

    store = TemplateStore(settings.templates.root)
    device = Device(account.ldplayer.adb_serial, settings.safety, settings.ldplayer.resolution)
    vision = Vision(store, device.screenshot, settings.vision, settings.templates.match_threshold)
    notifier = NullNotifier()
    safety = Safety(settings.safety, notifier, settings.logging.dir / "dry_run")
    recovery = Recovery(
        device,
        vision,
        notifier,
        settings.logging.dir / "unknown",
        app_in_foreground=lambda: device.app_in_foreground(settings.game.package_name),
    )
    ctx = TaskContext(
        device=device,
        vision=vision,
        safety=safety,
        recovery=recovery,
        notifier=notifier,
        settings=settings,
        account=account,
        log_dir=settings.logging.dir,
    )
    nav = Navigator(ctx, coords.farm, settings.farm)

    entries: list[dict[str, object]] = []
    known_rows: list[Image] = []
    try:
        nav.ensure_standing()
        nav.open_map()
        ensure_find_map_tab(nav, vision, device)

        idx = 0
        for page in range(args.max_pages):
            new_on_page = 0
            for i in range(geom.visible_rows):
                safety.check_kill_switch()
                # xem trước dòng có nội dung không — trang cuối hay hụt dòng
                pre = crop(vision.capture_screen(), row_rect(geom, i))
                if row_is_empty(pre):
                    continue
                if is_known_row(pre, known_rows):
                    continue
                device.tap(geom.row_x, geom.first_row_y + i * geom.row_pitch)
                time.sleep(PREVIEW_SETTLE_SECONDS)
                frame = vision.capture_screen()
                if not nav.map_is_open(frame):
                    # tap trượt làm màn đóng (hoặc popup đè) — mở lại rồi thử tiếp
                    log.warning("map screen lost, reopening", row=i, page=page)
                    nav.ensure_standing()
                    nav.open_map()
                    ensure_find_map_tab(nav, vision, device)
                    continue
                row_img = crop(frame, row_rect(geom, i))
                if is_known_row(row_img, known_rows):
                    continue  # dòng cũ hiện lại sau khi cuộn
                known_rows.append(pre)
                known_rows.append(row_img)  # lưu cả 2 trạng thái thường/highlight
                files = {
                    "map": f"{idx:03d}_map.png",
                    "row": f"{idx:03d}_row.png",
                    "title": f"{idx:03d}_title.png",
                }
                cv2.imwrite(str(out_dir / files["map"]), crop(frame, coords.farm.map_image_rect))
                cv2.imwrite(str(out_dir / files["row"]), row_img)
                if coords.farm.map_title_rect:
                    cv2.imwrite(
                        str(out_dir / files["title"]), crop(frame, coords.farm.map_title_rect)
                    )
                entries.append({"index": idx, "name": "", "files": files, "page": page})
                log.info("map harvested", index=idx, page=page, row=i)
                idx += 1
                new_on_page += 1
            if new_on_page == 0:
                log.info("no new rows on this page, stopping", page=page)
                break
            # cuộn gần một trang (chừa 1 dòng đè nhau để không hụt dòng giữa)
            lx, ly, lw, lh = geom.rect
            x = lx + lw // 2
            dy = (geom.visible_rows - 2) * geom.row_pitch
            device.swipe(x, ly + lh - 20, x, ly + lh - 20 - dy, duration_ms=600)
            time.sleep(1.2)

        manifest = {
            "template_version": settings.templates.version,
            "map_image_rect": list(coords.farm.map_image_rect),
            "maps": entries,
        }
        (out_dir / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        print(f"== harvested {len(entries)} maps -> {out_dir} ==")
        print("dien ten map vao manifest.yaml (hoac GUI sau nay) truoc khi dung")
        nav.close_map()
        return 0
    finally:
        device.close()


if __name__ == "__main__":
    sys.exit(main())
