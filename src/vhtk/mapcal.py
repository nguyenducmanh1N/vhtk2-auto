"""Pixel <-> in-game coordinate mapping for map preview images (R3 nav data).

Ảnh bản đồ trên màn bản đồ là bản thu nhỏ TUYẾN TÍNH của map thật, hai trục
độc lập (không xoay): ``game_x = sx * px + ox`` và ``game_y = sy * py + oy``.
Hiệu chuẩn = thu >= 2 cặp (pixel, toạ độ game) từ dòng "Đi: <Map>(x, y)" mà
game in ra khi tap điểm hợp lệ, rồi fit bình phương tối thiểu từng trục.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MapTransform:
    """Linear per-axis mapping between preview pixels and game coordinates."""

    sx: float
    ox: float
    sy: float
    oy: float

    def to_game(self, px: float, py: float) -> tuple[int, int]:
        """Preview pixel (screen coords) -> in-game coordinate, rounded."""
        return (round(self.sx * px + self.ox), round(self.sy * py + self.oy))

    def to_pixel(self, gx: float, gy: float) -> tuple[int, int]:
        """In-game coordinate -> preview pixel (screen coords), rounded."""
        return (round((gx - self.ox) / self.sx), round((gy - self.oy) / self.sy))


def _fit_axis(samples: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares fit of ``v = s * p + o`` for one axis.

    Raises:
        ValueError: fewer than 2 samples, or all pixels identical (slope
            undefined — the two calibration taps must not share the axis value).
    """
    if len(samples) < 2:
        raise ValueError("need at least 2 samples per axis")
    n = len(samples)
    mean_p = sum(p for p, _ in samples) / n
    mean_v = sum(v for _, v in samples) / n
    denom = sum((p - mean_p) ** 2 for p, _ in samples)
    if denom == 0:
        raise ValueError("all pixel values identical on one axis, cannot fit slope")
    s = sum((p - mean_p) * (v - mean_v) for p, v in samples) / denom
    return s, mean_v - s * mean_p


def fit_transform(pairs: list[tuple[tuple[int, int], tuple[int, int]]]) -> MapTransform:
    """Fit a :class:`MapTransform` from ((px, py), (gx, gy)) pairs.

    Raises:
        ValueError: not enough spread in the samples (see :func:`_fit_axis`).
    """
    sx, ox = _fit_axis([(float(p[0]), float(g[0])) for p, g in pairs])
    sy, oy = _fit_axis([(float(p[1]), float(g[1])) for p, g in pairs])
    return MapTransform(sx=sx, ox=ox, sy=sy, oy=oy)


def max_residual(
    t: MapTransform, pairs: list[tuple[tuple[int, int], tuple[int, int]]]
) -> float:
    """Largest |predicted - actual| game-coordinate error over the pairs.

    Sai số > ~3 đơn vị toạ độ nghĩa là có cặp nhập sai hoặc preview không
    tuyến tính — đừng lưu transform đó.
    """
    worst = 0.0
    for (px, py), (gx, gy) in pairs:
        pgx, pgy = t.to_game(px, py)
        worst = max(worst, abs(pgx - gx), abs(pgy - gy))
    return worst
