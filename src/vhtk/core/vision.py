"""Screen recognition layer: template matching, OCR, item-quality color detection.

Design notes:
- ``TemplateStore`` loads every PNG under ``templates/<version>/`` into memory
  once at startup; templates are addressed by relative path like
  ``"common/btn_confirm"``. Per-template overrides (threshold, ROI, grayscale,
  multi-scale) live in ``templates/<version>/manifest.yaml``.
- ``Vision`` never talks to a device directly: a ``capture`` callable returning
  a BGR screenshot is injected, so the whole layer is testable offline.
- All ``wait_*`` loops capture the screen once per poll cycle and reuse that
  frame for every template check in the cycle.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias, cast

import cv2
import numpy as np
import numpy.typing as npt
import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vhtk.config import ConfigError, QualitySettings, VisionSettings

log = structlog.get_logger(__name__)

Image: TypeAlias = npt.NDArray[np.uint8]
Rect: TypeAlias = tuple[int, int, int, int]
"""Rectangle as (x, y, w, h) in pixels."""
CaptureFn: TypeAlias = Callable[[], Image]

MANIFEST_NAME = "manifest.yaml"
# Chặn NMS nổ O(n^2) khi threshold quá thấp sinh hàng nghìn candidate
_MAX_NMS_CANDIDATES = 500


class VisionError(Exception):
    """Raised on invalid vision input (bad ROI, template larger than region)."""


@dataclass(frozen=True)
class Match:
    """One template match on a screenshot."""

    bbox: Rect
    score: float

    @property
    def center(self) -> tuple[int, int]:
        """Center point (x, y) of the bounding box — the tap target."""
        x, y, w, h = self.bbox
        return (x + w // 2, y + h // 2)


@dataclass
class Template:
    """A template image plus its matching metadata.

    ``threshold``/``grayscale`` set to ``None`` mean "use the global default".
    ``roi`` restricts the search region on the screenshot (big speed win for
    fixed-position UI like the close button in the top-right corner).
    ``scales`` enables multi-scale matching; rarely needed because every
    LDPlayer instance runs at the same 960x540 resolution.
    """

    name: str
    image: Image
    gray: Image
    threshold: float | None = None
    roi: Rect | None = None
    grayscale: bool | None = None
    scales: tuple[float, ...] | None = None


class _TemplateMeta(BaseModel):
    """Schema of one entry in ``templates/<version>/manifest.yaml``."""

    model_config = ConfigDict(extra="forbid")

    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    roi: Rect | None = None
    grayscale: bool | None = None
    scales: tuple[float, ...] | None = None


class TemplateStore:
    """Loads and caches every template image of one template version."""

    def __init__(self, root: Path) -> None:
        """Load all PNGs under ``root`` (e.g. ``templates/v1.0``) into memory.

        Raises:
            ConfigError: if ``root`` does not exist, an image cannot be
                decoded, or the manifest is invalid.
        """
        if not root.is_dir():
            raise ConfigError(f"template directory not found: {root}")
        self._root = root
        meta = self._load_manifest(root / MANIFEST_NAME)
        self._templates: dict[str, Template] = {}
        for png in sorted(root.rglob("*.png")):
            name = png.relative_to(root).with_suffix("").as_posix()
            image = cast("Image | None", cv2.imread(str(png), cv2.IMREAD_COLOR))
            if image is None:
                raise ConfigError(f"cannot decode template image: {png}")
            gray = cast(Image, cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
            m = meta.get(name, _TemplateMeta())
            self._templates[name] = Template(
                name=name,
                image=image,
                gray=gray,
                threshold=m.threshold,
                roi=m.roi,
                grayscale=m.grayscale,
                scales=m.scales,
            )
        for orphan in sorted(set(meta) - set(self._templates)):
            log.warning("manifest entry without image", template=orphan)
        if self._templates:
            log.info("templates loaded", count=len(self._templates), root=str(root))
        else:
            log.warning("template store is empty", root=str(root))

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, _TemplateMeta]:
        if not path.is_file():
            return {}
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must be a mapping of template name -> metadata")
        try:
            return {str(k): _TemplateMeta.model_validate(v) for k, v in raw.items()}
        except ValidationError as exc:
            raise ConfigError(f"invalid template manifest {path}:\n{exc}") from exc

    @property
    def names(self) -> list[str]:
        """All loaded template names, e.g. ``["common/btn_confirm", ...]``."""
        return sorted(self._templates)

    def __contains__(self, name: str) -> bool:
        return name in self._templates

    def get(self, name: str) -> Template:
        """Return a template by name like ``"common/btn_confirm"``.

        Raises:
            KeyError: if the template was not found on disk at load time.
        """
        try:
            return self._templates[name]
        except KeyError:
            raise KeyError(
                f"template not found: {name!r} "
                f"(store {self._root} has {len(self._templates)} templates)"
            ) from None


def _to_gray(img: Image) -> Image:
    if img.ndim == 2:
        return img
    return cast(Image, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))


def _crop_roi(img: Image, roi: Rect | None) -> tuple[Image, tuple[int, int]]:
    """Crop ``img`` to ``roi`` (clamped), returning the crop and its offset."""
    if roi is None:
        return img, (0, 0)
    x, y, w, h = roi
    ih, iw = img.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(iw, x + w), min(ih, y + h)
    if x1 <= x0 or y1 <= y0:
        raise VisionError(f"roi {roi} lies outside screenshot {iw}x{ih}")
    return img[y0:y1, x0:x1], (x0, y0)


def best_match(haystack: Image, needle: Image) -> tuple[float, Rect]:
    """Best match of ``needle`` in ``haystack`` regardless of any threshold.

    Both images must have the same number of channels. Returns
    ``(score, bbox)`` with score in [-1, 1] (TM_CCOEFF_NORMED).
    """
    result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
    _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)
    h, w = needle.shape[:2]
    return float(max_val), (int(max_loc[0]), int(max_loc[1]), w, h)


def _iou(a: Rect, b: Rect) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter == 0:
        return 0.0
    return inter / float(aw * ah + bw * bh - inter)


def non_max_suppression(matches: Sequence[Match], overlap_threshold: float) -> list[Match]:
    """Greedy NMS: keep highest-score matches, drop ones overlapping a kept
    match with IoU above ``overlap_threshold``. Result is sorted by score desc.
    """
    kept: list[Match] = []
    for cand in sorted(matches, key=lambda m: m.score, reverse=True):
        if all(_iou(cand.bbox, k.bbox) <= overlap_threshold for k in kept):
            kept.append(cand)
    return kept


class Vision:
    """Template matching against screenshots from an injected capture source."""

    def __init__(
        self,
        store: TemplateStore,
        capture: CaptureFn,
        cfg: VisionSettings,
        default_threshold: float,
    ) -> None:
        """Args:
        store: preloaded template store for the active template version.
        capture: callable returning a fresh BGR screenshot (injected so the
            layer works with any device backend and is testable offline).
        cfg: vision section of settings.yaml.
        default_threshold: global match threshold (templates.match_threshold).
        """
        self._store = store
        self._capture = capture
        self._cfg = cfg
        self._default_threshold = default_threshold

    @property
    def store(self) -> TemplateStore:
        """The underlying template store."""
        return self._store

    def capture_screen(self) -> Image:
        """Take one fresh screenshot.

        Callers doing several ``find`` calls in one cycle should capture once
        and pass the frame via ``screenshot=`` instead of re-capturing.
        """
        return self._capture()

    def threshold_for(self, template_name: str, override: float | None = None) -> float:
        """Effective threshold: explicit override > per-template > global."""
        if override is not None:
            return override
        tpl = self._store.get(template_name)
        return tpl.threshold if tpl.threshold is not None else self._default_threshold

    def _prepare(self, screenshot: Image, tpl: Template) -> tuple[Image, Image, tuple[int, int]]:
        """Return (haystack, base needle, roi offset) honoring ROI/grayscale."""
        use_gray = tpl.grayscale if tpl.grayscale is not None else self._cfg.grayscale_match
        hay, offset = _crop_roi(screenshot, tpl.roi)
        if use_gray:
            return _to_gray(hay), tpl.gray, offset
        return hay, tpl.image, offset

    @staticmethod
    def _scaled(needle: Image, scale: float) -> Image:
        if scale == 1.0:
            return needle
        return cast(
            Image,
            cv2.resize(needle, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA),
        )

    def locate(self, template_name: str, screenshot: Image | None = None) -> Match:
        """Best match position and score, regardless of threshold.

        Raises:
            VisionError: if the template does not fit in its search region
                at any configured scale.
        """
        shot = screenshot if screenshot is not None else self.capture_screen()
        tpl = self._store.get(template_name)
        hay, needle_base, (ox, oy) = self._prepare(shot, tpl)
        best: Match | None = None
        for scale in tpl.scales or (1.0,):
            needle = self._scaled(needle_base, scale)
            if needle.shape[0] > hay.shape[0] or needle.shape[1] > hay.shape[1]:
                continue
            score, (bx, by, bw, bh) = best_match(hay, needle)
            if best is None or score > best.score:
                best = Match(bbox=(bx + ox, by + oy, bw, bh), score=score)
        if best is None:
            raise VisionError(
                f"template {template_name!r} larger than its search region at every scale"
            )
        return best

    def find(
        self,
        template_name: str,
        screenshot: Image | None = None,
        threshold: float | None = None,
    ) -> Match | None:
        """Find one template on screen; ``None`` if best score is below threshold."""
        m = self.locate(template_name, screenshot)
        if m.score >= self.threshold_for(template_name, threshold):
            return m
        return None

    def find_all(
        self,
        template_name: str,
        screenshot: Image | None = None,
        threshold: float | None = None,
    ) -> list[Match]:
        """All non-overlapping matches of a template, best score first.

        Overlapping candidates are collapsed by non-maximum suppression using
        ``vision.nms_overlap_threshold`` from settings.
        """
        shot = screenshot if screenshot is not None else self.capture_screen()
        tpl = self._store.get(template_name)
        thr = self.threshold_for(template_name, threshold)
        hay, needle_base, (ox, oy) = self._prepare(shot, tpl)
        candidates: list[Match] = []
        for scale in tpl.scales or (1.0,):
            needle = self._scaled(needle_base, scale)
            nh, nw = needle.shape[:2]
            if nh > hay.shape[0] or nw > hay.shape[1]:
                continue
            result = cv2.matchTemplate(hay, needle, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.nonzero(result >= thr)
            for xx, yy in zip(xs.tolist(), ys.tolist(), strict=True):
                candidates.append(
                    Match(bbox=(xx + ox, yy + oy, nw, nh), score=float(result[yy, xx]))
                )
        if len(candidates) > _MAX_NMS_CANDIDATES:
            candidates = sorted(candidates, key=lambda m: m.score, reverse=True)
            candidates = candidates[:_MAX_NMS_CANDIDATES]
        return non_max_suppression(candidates, self._cfg.nms_overlap_threshold)

    def wait_for(
        self,
        template_name: str,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> Match | None:
        """Poll until the template appears; ``None`` on timeout."""
        hit = self.wait_any([template_name], timeout=timeout, interval=interval)
        return hit[1] if hit is not None else None

    def wait_any(
        self,
        template_names: Sequence[str],
        timeout: float | None = None,
        interval: float | None = None,
    ) -> tuple[str, Match] | None:
        """Poll until ANY of the templates appears.

        Core primitive for "one of several screens may show up next" (e.g.
        after login: main screen, event popup, or maintenance notice). Each
        poll cycle captures the screen once and checks all templates against
        that single frame. Returns ``(template_name, match)`` or ``None``.
        """
        deadline = time.monotonic() + (
            timeout if timeout is not None else self._cfg.default_timeout
        )
        sleep_s = interval if interval is not None else self._cfg.poll_interval
        while True:
            shot = self.capture_screen()
            for name in template_names:
                m = self.find(name, screenshot=shot)
                if m is not None:
                    return name, m
            if time.monotonic() >= deadline:
                log.debug("wait_any timeout", templates=list(template_names))
                return None
            time.sleep(sleep_s)

    def wait_vanish(
        self,
        template_name: str,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> bool:
        """Poll until the template disappears. ``True`` if it vanished in time."""
        deadline = time.monotonic() + (
            timeout if timeout is not None else self._cfg.default_timeout
        )
        sleep_s = interval if interval is not None else self._cfg.poll_interval
        while True:
            if self.find(template_name) is None:
                return True
            if time.monotonic() >= deadline:
                log.debug("wait_vanish timeout", template=template_name)
                return False
            time.sleep(sleep_s)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


def preprocess_for_ocr(img: Image, upscale: float = 3.0, invert: bool | None = None) -> Image:
    """Prepare a small UI crop for OCR: grayscale, upscale, binarize.

    In-game numbers are tiny; raw OCR misreads them badly. Upscaling with
    cubic interpolation plus Otsu binarization fixes most of it. With
    ``invert=None`` the image is auto-inverted when it looks like light text
    on a dark background (OCR engines prefer dark-on-light).
    """
    gray = _to_gray(img)
    if upscale != 1.0:
        gray = cast(
            Image,
            cv2.resize(gray, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC),
        )
    _thr, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if invert is None:
        # nền tối chiếm đa số → chữ đang sáng trên nền tối → đảo lại
        invert = float(binary.mean()) < 127
    if invert:
        binary = cv2.bitwise_not(binary)
    return cast(Image, binary)


def parse_number(text: str) -> int | None:
    """Parse the first integer from an OCR string.

    Handles OCR junk around the number, Vietnamese thousand separators
    (``1.234.567`` / ``1,234``), and K/M suffixes for abbreviated in-game
    display (``12K`` -> 12000, ``1,5M`` -> 1500000). Without a suffix every
    ``.``/``,`` is treated as a thousand separator.
    """
    m = re.search(r"(\d[\d.,]*)\s?([kKmM])?", text)
    if m is None:
        return None
    raw = m.group(1).rstrip(".,")
    suffix = m.group(2)
    if suffix is None:
        return int(raw.replace(".", "").replace(",", ""))
    # có hậu tố rút gọn → dấu . / , là dấu thập phân
    norm = raw.replace(",", ".")
    if norm.count(".") > 1:
        norm = norm.replace(".", "", norm.count(".") - 1)
    try:
        value = float(norm)
    except ValueError:
        return None
    multiplier = 1_000 if suffix in ("k", "K") else 1_000_000
    return round(value * multiplier)


def parse_fraction(text: str) -> tuple[int, int] | None:
    """Parse ``"56/60"``-style OCR text (e.g. bag slots) into ``(56, 60)``.

    Tolerates thousand separators and ``|``/``\\`` misread as the slash.
    """
    normalized = text.replace("|", "/").replace("\\", "/")
    m = re.search(r"(\d[\d.,]*)\s*/\s*(\d[\d.,]*)", normalized)
    if m is None:
        return None
    strip = str.maketrans("", "", ".,")
    return int(m.group(1).translate(strip)), int(m.group(2).translate(strip))


def _extract_texts(result: Any) -> list[str]:
    """Pull recognized strings out of a PaddleOCR result.

    Supports both the classic 2.x nested-list format (``[box, (text, conf)]``)
    and the 3.x dict format (``{"rec_texts": [...]}``).
    """
    texts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for t in node.get("rec_texts", []):
                texts.append(str(t))
            return
        if isinstance(node, (list, tuple)):
            if (
                len(node) == 2
                and isinstance(node[0], (list, tuple))
                and isinstance(node[1], (list, tuple))
                and len(node[1]) >= 1
                and isinstance(node[1][0], str)
            ):
                texts.append(node[1][0])
                return
            for child in node:
                walk(child)

    walk(result)
    return texts


class OCR:
    """Thin PaddleOCR wrapper with lazy initialization.

    PaddleOCR is heavy (model download + load); the engine is created on the
    first read, not at construction time.
    """

    def __init__(self, lang: str = "vi") -> None:
        self._lang = lang
        self._engine: Any = None

    def _get_engine(self) -> Any:
        if self._engine is None:
            log.info("initializing paddleocr", lang=self._lang)
            from paddleocr import PaddleOCR  # import nặng — chỉ khi thật sự dùng OCR

            try:
                self._engine = PaddleOCR(lang=self._lang, use_angle_cls=False, show_log=False)
            except TypeError:
                # PaddleOCR 3.x đổi tên tham số — fallback về mặc định
                self._engine = PaddleOCR(lang=self._lang)
        return self._engine

    def read_text(self, roi: Image, preprocess: bool = True) -> str:
        """OCR a cropped region and return all recognized text joined by spaces."""
        img = preprocess_for_ocr(roi) if preprocess else roi
        if img.ndim == 2:
            img = cast(Image, cv2.cvtColor(img, cv2.COLOR_GRAY2BGR))
        result = self._get_engine().ocr(img)
        return " ".join(_extract_texts(result)).strip()

    def read_number(self, roi: Image) -> int | None:
        """Read an integer (gold amount, durability...); ``None`` if unreadable."""
        return parse_number(self.read_text(roi))

    def read_fraction(self, roi: Image) -> tuple[int, int] | None:
        """Read ``current/total`` (e.g. bag slots ``56/60``); ``None`` if unreadable."""
        return parse_fraction(self.read_text(roi))


# ---------------------------------------------------------------------------
# Item quality by border color
# ---------------------------------------------------------------------------


def detect_quality(cell: Image, cfg: QualitySettings) -> str | None:
    """Detect item quality from the border color of an item-cell crop.

    Samples a ring of ``cfg.border_width`` pixels around the cell edge,
    converts to HSV and counts pixels falling into each configured color
    range. Returns the quality name (key of ``cfg.colors``) with the highest
    pixel fraction, or ``None`` when no range reaches
    ``cfg.min_match_fraction``.
    """
    h, w = cell.shape[:2]
    if h < 4 or w < 4:
        return None
    bw = max(1, min(cfg.border_width, h // 2, w // 2))
    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
    ring_mask = np.zeros((h, w), dtype=bool)
    ring_mask[:bw, :] = True
    ring_mask[-bw:, :] = True
    ring_mask[:, :bw] = True
    ring_mask[:, -bw:] = True
    ring = hsv[ring_mask]
    hue = ring[:, 0].astype(int)
    sat = ring[:, 1].astype(int)
    val = ring[:, 2].astype(int)
    total = len(ring)
    best_name: str | None = None
    best_frac = 0.0
    for name, rng in cfg.colors.items():
        h_lo, h_hi = rng.h
        # hue là vòng tròn 0..179 — cho phép dải "quấn vòng" (h_lo > h_hi)
        h_ok = (hue >= h_lo) & (hue <= h_hi) if h_lo <= h_hi else (hue >= h_lo) | (hue <= h_hi)
        sel = h_ok & (sat >= rng.s[0]) & (sat <= rng.s[1]) & (val >= rng.v[0]) & (val <= rng.v[1])
        frac = float(np.count_nonzero(sel)) / total
        if frac > best_frac:
            best_name, best_frac = name, frac
    if best_frac < cfg.min_match_fraction:
        return None
    return best_name
