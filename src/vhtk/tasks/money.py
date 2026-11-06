"""Read the character's money (vàng / bạc / đồng) off the bag window (W3).

The three figures sit on one row under the bag grid; each is OCR'd from its
own rectangle (``sell.money_cells`` in coordinates.yaml). The reader is
deliberately forgiving: no PaddleOCR installed, unmeasured cells or an
unreadable frame all yield ``None`` fields — callers treat ``None`` as
"unknown", never as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from vhtk.config import Currency, MoneyCells
from vhtk.core.vision import Image

log = structlog.get_logger(__name__)

# ngôn ngữ OCR đã khởi tạo hỏng trong tiến trình này (thiếu paddlepaddle...)
_ocr_unavailable: set[str] = set()


class NumberReader(Protocol):
    def read_number(self, roi: Image) -> int | None: ...


@dataclass(frozen=True)
class Money:
    """One reading of the money row; ``None`` = that figure was unreadable."""

    vang: int | None = None
    bac: int | None = None
    dong: int | None = None

    def get(self, currency: Currency) -> int | None:
        return getattr(self, currency)  # type: ignore[no-any-return]

    def as_dict(self) -> dict[str, int | None]:
        return {"vang": self.vang, "bac": self.bac, "dong": self.dong}


def _crop(frame: Image, rect: tuple[int, int, int, int]) -> Image | None:
    x, y, w, h = rect
    if w <= 0 or h <= 0:
        return None
    roi = frame[y : y + h, x : x + w]
    return roi if roi.size else None


class MoneyReader:
    """OCR the money row; swallows every OCR failure into ``None``."""

    def __init__(
        self, cells: MoneyCells | None, ocr: NumberReader | None, lang: str = "vi"
    ) -> None:
        self._cells = cells
        self._ocr = ocr
        self._lang = lang
        self._ocr_failed = False

    @property
    def available(self) -> bool:
        """Cells are measured and OCR has not failed to initialize."""
        return self._cells is not None and not self._ocr_failed

    def _engine(self) -> NumberReader | None:
        if self._ocr is None and not self._ocr_failed:
            if self._lang in _ocr_unavailable:
                # đã hỏng từ chuyến bán trước — không thử lại (mỗi lần thử ~10s)
                self._ocr_failed = True
                return None
            try:
                from vhtk.core.vision import OCR

                ocr = OCR(self._lang)
                ocr.ensure_ready()
                self._ocr = ocr
            except Exception as exc:  # paddleocr thiếu/kén máy — không được làm hỏng task
                self._ocr_failed = True
                _ocr_unavailable.add(self._lang)
                log.warning("ocr unavailable, money reading disabled", error=str(exc))
        return self._ocr

    def read(self, frame: Image) -> Money:
        """Read all three figures from a bag-open frame."""
        if self._cells is None:
            return Money()
        engine = self._engine()
        if engine is None:
            return Money()
        values: dict[str, int | None] = {}
        for currency in ("vang", "bac", "dong"):
            roi = _crop(frame, self._cells.rect(currency))
            if roi is None:
                values[currency] = None
                continue
            try:
                values[currency] = engine.read_number(roi)
            except Exception as exc:  # OCR nổ ở runtime (model lỗi) — coi như không đọc được
                self._ocr_failed = True
                log.warning("ocr failed, money reading disabled", error=str(exc))
                return Money()
        money = Money(**values)
        log.info("money read", **money.as_dict())
        return money


def reached_threshold(money: Money, currency: Currency, threshold: int) -> bool:
    """Gold-threshold decision: only a READ figure can trigger a transfer."""
    value = money.get(currency)
    return value is not None and threshold > 0 and value >= threshold


def transfer_amount(balance: int, keep: int) -> int:
    """Amount to hand over: everything above ``keep`` (never negative)."""
    return max(0, balance - keep)


def transfer_verified(
    sender_before: int | None,
    sender_after: int | None,
    receiver_before: int | None,
    receiver_after: int | None,
    amount: int,
    tolerance: int,
) -> bool:
    """Both sides' deltas must match ``amount`` within ``tolerance``.

    Any unreadable figure fails verification — a trade is only counted as
    done when the money is SEEN moving on both screens (PLAN.md W3).
    """
    if None in (sender_before, sender_after, receiver_before, receiver_after):
        return False
    assert sender_before is not None and sender_after is not None
    assert receiver_before is not None and receiver_after is not None
    sent = sender_before - sender_after
    received = receiver_after - receiver_before
    return abs(sent - amount) <= tolerance and abs(received - amount) <= tolerance


def money_fields(data: dict[str, Any] | None) -> Money:
    """Rebuild a ``Money`` from a state-file dict (missing/garbage -> None)."""
    if not isinstance(data, dict):
        return Money()
    out: dict[str, int | None] = {}
    for key in ("vang", "bac", "dong"):
        v = data.get(key)
        out[key] = v if isinstance(v, int) and not isinstance(v, bool) else None
    return Money(**out)
