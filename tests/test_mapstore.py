"""Tests for the harvested map store (vhtk/mapstore.py) — manifest parsing,
image cache, text-mask matching helpers. Device-free, synthetic images."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from vhtk.mapstore import MapStore, flexible_match, maps_dir, text_mask


def row_image(seed: int, size: tuple[int, int] = (24, 200)) -> np.ndarray:
    """A dark row with a bright, seed-specific "text" pattern."""
    rng = np.random.default_rng(seed)
    img = np.full((*size, 3), 30, dtype=np.uint8)
    for i in range(8):
        x = 10 + i * 22 + int(rng.integers(0, 6))
        img[6:18, x : x + 10] = 255 if (seed + i) % 2 == 0 else 30
    return img


def build_store(root: Path, *, with_unnamed: bool = True) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    maps = []
    for idx, name in [(0, "Tuyền Châu"), (1, "Dã Trư lâm")]:
        files = {"map": f"{idx:03d}_map.png", "row": f"{idx:03d}_row.png",
                 "title": f"{idx:03d}_title.png"}
        for key, fname in files.items():
            cv2.imwrite(str(root / fname), row_image(idx * 10 + len(key)))
        entry: dict = {"index": idx, "name": name, "files": files, "page": 0}
        if idx == 0:
            entry["calibration"] = {"sx": 0.09375, "ox": 126.3, "sy": 0.0873, "oy": 163.8,
                                    "pairs": 3, "max_residual": 1.0}
        maps.append(entry)
    if with_unnamed:
        maps.append({"index": 2, "name": None, "files": {"map": "x.png", "row": "x.png",
                                                         "title": "x.png"}, "page": 1})
    (root / "manifest.yaml").write_text(
        yaml.safe_dump({"template_version": "test", "maps": maps}, allow_unicode=True),
        encoding="utf-8",
    )
    return {"root": root}


def test_store_loads_named_entries_only(tmp_path: Path) -> None:
    build_store(tmp_path)
    store = MapStore(tmp_path)
    assert store.names() == ["Tuyền Châu", "Dã Trư lâm"]
    assert "Tuyền Châu" in store
    assert store.get("Không Tồn Tại") is None


def test_store_calibration_parsed_as_transform(tmp_path: Path) -> None:
    build_store(tmp_path)
    store = MapStore(tmp_path)
    calibrated = store.get("Tuyền Châu")
    uncalibrated = store.get("Dã Trư lâm")
    assert calibrated is not None and calibrated.transform is not None
    # quy đổi hai chiều phải khớp ngược nhau — sai số tối đa 1 đơn vị toạ độ
    # game do làm tròn, tức ~1/sx pixel (~11px với scale Tuyền Châu)
    gx, gy = calibrated.transform.to_game(587, 262)
    px, py = calibrated.transform.to_pixel(gx, gy)
    assert abs(px - 587) <= 1 / 0.09375 and abs(py - 262) <= 1 / 0.0873
    assert uncalibrated is not None and uncalibrated.transform is None


def test_store_missing_manifest_is_empty(tmp_path: Path) -> None:
    store = MapStore(tmp_path / "khong_ton_tai")
    assert store.names() == []
    assert store.get("Tuyền Châu") is None


def test_store_image_cached_and_missing_raises(tmp_path: Path) -> None:
    build_store(tmp_path)
    store = MapStore(tmp_path)
    entry = store.get("Tuyền Châu")
    assert entry is not None
    img1 = store.image(entry.row_file)
    img2 = store.image(entry.row_file)
    assert img1 is img2  # cache trả đúng cùng object
    with pytest.raises(FileNotFoundError):
        store.image(tmp_path / "khong_co.png")


def test_maps_dir_follows_template_version(tmp_path: Path) -> None:
    assert maps_dir("v1.0-avd", root=tmp_path) == tmp_path / "v1.0-avd"


def test_text_mask_ignores_highlight_background() -> None:
    """Dòng đang chọn có nền highlight — mặt nạ chữ phải triệt tiêu khác biệt
    nền, chỉ giữ pixel chữ sáng (cơ chế match 1.00/0.37 đo trên AVD)."""
    row = row_image(7)
    highlighted = row.copy()
    highlighted[highlighted[:, :, 0] == 30] = 120  # nền sáng lên vì highlight
    assert flexible_match(text_mask(highlighted), text_mask(row)) >= 0.99


def test_flexible_match_swaps_and_rejects_incompatible() -> None:
    big = row_image(3)
    small = big[4:20, 8:120]
    # ảnh nhỏ nằm trong ảnh lớn — chiều nào làm needle cũng phải match
    assert flexible_match(big, small) >= 0.99
    assert flexible_match(small, big) >= 0.99
    # mỗi chiều lớn nhỏ một kiểu = rect đo sai — không match được
    tall_thin = np.zeros((100, 10, 3), dtype=np.uint8)
    short_wide = np.zeros((10, 100, 3), dtype=np.uint8)
    assert flexible_match(tall_thin, short_wide) == 0.0
