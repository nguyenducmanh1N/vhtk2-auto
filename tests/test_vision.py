"""Tests for the pure-logic parts of vhtk.core.vision (no device, no PaddleOCR)."""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from vhtk.config import load_settings
from vhtk.core.vision import (
    Match,
    TemplateStore,
    Vision,
    VisionError,
    color_ratio,
    detect_quality,
    non_max_suppression,
    parse_fraction,
    parse_number,
    preprocess_for_ocr,
)

SETTINGS = load_settings(Path(__file__).parent.parent / "config" / "settings.yaml")


# ---------------------------------------------------------------------------
# Non-maximum suppression
# ---------------------------------------------------------------------------


def _m(x: int, y: int, w: int, h: int, score: float) -> Match:
    return Match(bbox=(x, y, w, h), score=score)


def test_nms_suppresses_overlapping_matches() -> None:
    matches = [
        _m(10, 10, 20, 20, 0.90),
        _m(12, 11, 20, 20, 0.85),  # chồng gần hết lên match đầu → phải bị loại
        _m(100, 100, 20, 20, 0.70),
    ]
    kept = non_max_suppression(matches, overlap_threshold=0.3)
    assert [k.score for k in kept] == [0.90, 0.70]


def test_nms_keeps_distant_matches_and_sorts_by_score() -> None:
    matches = [_m(0, 0, 10, 10, 0.5), _m(50, 50, 10, 10, 0.9)]
    kept = non_max_suppression(matches, overlap_threshold=0.3)
    assert [k.score for k in kept] == [0.9, 0.5]


def test_nms_empty_input() -> None:
    assert non_max_suppression([], overlap_threshold=0.3) == []


def test_match_center() -> None:
    assert _m(100, 50, 40, 30, 1.0).center == (120, 65)


# ---------------------------------------------------------------------------
# OCR text parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.234.567", 1_234_567),
        ("1,234", 1_234),
        ("56", 56),
        ("vàng: 9.999", 9_999),
        ("12K", 12_000),
        ("12k", 12_000),
        ("1.5K", 1_500),
        ("1,5M", 1_500_000),
        ("2M", 2_000_000),
        ("x 123.", 123),  # dấu chấm rác cuối chuỗi
        ("", None),
        ("abc", None),
        ("N/A", None),
    ],
)
def test_parse_number(text: str, expected: int | None) -> None:
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("56/60", (56, 60)),
        ("56 / 60", (56, 60)),
        ("1.234/9.999", (1234, 9999)),
        ("56|60", (56, 60)),  # OCR đọc nhầm / thành |
        ("no digits here", None),
        ("", None),
    ],
)
def test_parse_fraction(text: str, expected: tuple[int, int] | None) -> None:
    assert parse_fraction(text) == expected


def test_preprocess_inverts_light_text_on_dark_background() -> None:
    img = np.full((20, 60, 3), 20, dtype=np.uint8)
    cv2.putText(img, "123", (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    out = preprocess_for_ocr(img)
    assert out.ndim == 2
    assert out.shape == (60, 180)  # upscale 3x
    # sau auto-invert phải là chữ tối trên nền sáng → đa số pixel trắng
    assert float(out.mean()) > 127


# ---------------------------------------------------------------------------
# Quality detection by border color
# ---------------------------------------------------------------------------


def _cell_with_border(hsv: tuple[int, int, int], size: int = 40, border: int = 3) -> np.ndarray:
    pixel = np.uint8([[list(hsv)]])
    bgr = cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0]
    cell = np.full((size, size, 3), 45, dtype=np.uint8)  # ruột ô màu xám tối
    cell[:border, :] = bgr
    cell[-border:, :] = bgr
    cell[:, :border] = bgr
    cell[:, -border:] = bgr
    return cell


@pytest.mark.parametrize(
    ("quality", "hsv"),
    [
        ("trắng", (0, 10, 230)),
        ("lục", (60, 200, 200)),
        ("lam", (110, 200, 200)),
        ("tím", (140, 180, 180)),
        ("cam", (18, 200, 220)),
    ],
)
def test_detect_quality_known_colors(quality: str, hsv: tuple[int, int, int]) -> None:
    cell = _cell_with_border(hsv)
    assert detect_quality(cell, SETTINGS.quality) == quality


def test_detect_quality_unknown_color_returns_none() -> None:
    cell = np.full((40, 40, 3), 45, dtype=np.uint8)  # toàn xám tối, không có viền màu
    assert detect_quality(cell, SETTINGS.quality) is None


def test_detect_quality_tiny_cell_returns_none() -> None:
    cell = np.zeros((2, 2, 3), dtype=np.uint8)
    assert detect_quality(cell, SETTINGS.quality) is None


# ---------------------------------------------------------------------------
# TemplateStore + Vision on synthetic screenshots
# ---------------------------------------------------------------------------

PATCH_RECT = (100, 50, 40, 30)  # x, y, w, h của patch trong ảnh gốc


@pytest.fixture()
def store_env(tmp_path: Path) -> tuple[TemplateStore, np.ndarray]:
    rng = np.random.default_rng(7)
    shot = rng.integers(0, 256, size=(200, 300, 3), dtype=np.uint8)
    x, y, w, h = PATCH_RECT
    patch = shot[y : y + h, x : x + w]

    root = tmp_path / "v1.0"
    (root / "common").mkdir(parents=True)
    for name in ("patch", "patch_roi", "patch_far"):
        cv2.imwrite(str(root / "common" / f"{name}.png"), patch)
    manifest = {
        "common/patch": {"threshold": 0.9},
        "common/patch_roi": {"roi": [90, 40, 60, 50]},  # ROI bao quanh patch
        "common/patch_far": {"roi": [0, 0, 50, 50]},  # ROI không chứa patch
    }
    (root / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return TemplateStore(root), shot


def _vision(store: TemplateStore, shot: np.ndarray) -> Vision:
    return Vision(store, capture=lambda: shot, cfg=SETTINGS.vision, default_threshold=0.9)


def test_store_loads_templates_and_manifest(store_env: tuple[TemplateStore, np.ndarray]) -> None:
    store, _shot = store_env
    assert store.names == ["common/patch", "common/patch_far", "common/patch_roi"]
    assert store.get("common/patch").threshold == 0.9
    assert store.get("common/patch_roi").roi == (90, 40, 60, 50)
    with pytest.raises(KeyError, match="nonexistent"):
        store.get("common/nonexistent")


def test_find_returns_exact_location(store_env: tuple[TemplateStore, np.ndarray]) -> None:
    store, shot = store_env
    m = _vision(store, shot).find("common/patch", screenshot=shot)
    assert m is not None
    assert m.bbox == PATCH_RECT
    assert m.score > 0.99


def test_find_with_roi_returns_global_coordinates(
    store_env: tuple[TemplateStore, np.ndarray],
) -> None:
    store, shot = store_env
    m = _vision(store, shot).find("common/patch_roi", screenshot=shot)
    assert m is not None
    assert m.bbox == PATCH_RECT  # toạ độ phải quy về hệ toàn màn hình, không phải hệ ROI


def test_find_misses_when_roi_excludes_target(
    store_env: tuple[TemplateStore, np.ndarray],
) -> None:
    store, shot = store_env
    assert _vision(store, shot).find("common/patch_far", screenshot=shot) is None


def test_find_all_with_nms(store_env: tuple[TemplateStore, np.ndarray]) -> None:
    store, shot = store_env
    x, y, w, h = PATCH_RECT
    patch = shot[y : y + h, x : x + w]
    canvas = shot.copy()
    canvas[120 : 120 + h, 200 : 200 + w] = patch  # bản sao thứ hai của patch
    matches = _vision(store, canvas).find_all("common/patch", screenshot=canvas)
    assert len(matches) == 2
    assert sorted(m.bbox[:2] for m in matches) == [(100, 50), (200, 120)]


def test_wait_any_and_wait_vanish(store_env: tuple[TemplateStore, np.ndarray]) -> None:
    store, shot = store_env
    vis = _vision(store, shot)
    hit = vis.wait_any(["common/patch_far", "common/patch"], timeout=1.0, interval=0.05)
    assert hit is not None
    assert hit[0] == "common/patch"
    # patch vẫn hiện trên màn → wait_vanish phải timeout
    assert vis.wait_vanish("common/patch", timeout=0.2, interval=0.05) is False

    rng = np.random.default_rng(99)
    other = rng.integers(0, 256, size=(200, 300, 3), dtype=np.uint8)
    vis_other = _vision(store, other)
    assert vis_other.find("common/patch") is None
    assert vis_other.wait_vanish("common/patch", timeout=0.5, interval=0.05) is True
    assert vis_other.wait_for("common/patch", timeout=0.2, interval=0.05) is None


# ---------------------------------------------------------------------------
# color_ratio — chỉ dấu HUD phân biệt bằng màu (icon Túi đồ "Đầy")
# ---------------------------------------------------------------------------


def test_color_ratio_counts_red_fraction() -> None:
    red = SETTINGS.farm.bag_full_color
    canvas = np.zeros((100, 100, 3), dtype=np.uint8)  # đen — ngoài dải màu
    # tô đỏ thuần (BGR) đúng nửa trái của rect (20, 20, 40, 40)
    canvas[20:60, 20:40] = (0, 0, 255)
    ratio = color_ratio(canvas, (20, 20, 40, 40), red)
    assert ratio == pytest.approx(0.5, abs=0.01)
    assert color_ratio(canvas, (60, 60, 30, 30), red) == 0.0


def test_color_ratio_rect_outside_frame_raises() -> None:
    canvas = np.zeros((50, 50, 3), dtype=np.uint8)
    with pytest.raises(VisionError):
        color_ratio(canvas, (40, 40, 20, 20), SETTINGS.farm.bag_full_color)
