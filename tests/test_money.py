"""Tests for money reading + the W3 pure decisions (no device, fake OCR)."""

from __future__ import annotations

import numpy as np

from vhtk.config import MoneyCells
from vhtk.tasks.money import (
    Money,
    MoneyReader,
    money_fields,
    reached_threshold,
    transfer_amount,
    transfer_verified,
)

CELLS = MoneyCells(vang=(10, 10, 20, 10), bac=(40, 10, 20, 10), dong=(70, 10, 20, 10))


class FakeOCR:
    """Returns a number per x-origin of the ROI it is handed (identified by width/height stamp)."""

    def __init__(self, values: dict[int, int | None]) -> None:
        self.values = values
        self.calls = 0

    def read_number(self, roi: np.ndarray) -> int | None:
        self.calls += 1
        # mỗi ô được tô bằng chính x-origin để fake phân biệt
        return self.values[int(roi[0, 0, 0])]


def frame_with_cells() -> np.ndarray:
    frame = np.zeros((40, 100, 3), dtype=np.uint8)
    for x, _y, w, h in (CELLS.vang, CELLS.bac, CELLS.dong):
        frame[10 : 10 + h, x : x + w] = x
    return frame


def test_reader_reads_all_three_cells() -> None:
    ocr = FakeOCR({10: 1, 40: 250, 70: 99})
    reader = MoneyReader(CELLS, ocr)
    assert reader.available
    assert reader.read(frame_with_cells()) == Money(vang=1, bac=250, dong=99)
    assert ocr.calls == 3


def test_reader_without_cells_is_unavailable() -> None:
    reader = MoneyReader(None, FakeOCR({}))
    assert not reader.available
    assert reader.read(frame_with_cells()) == Money()


def test_reader_swallows_ocr_crash_and_disables_itself() -> None:
    class Boom:
        def read_number(self, roi: np.ndarray) -> int | None:
            raise RuntimeError("model missing")

    reader = MoneyReader(CELLS, Boom())
    assert reader.read(frame_with_cells()) == Money()
    assert not reader.available


def test_reached_threshold_only_on_read_values() -> None:
    assert reached_threshold(Money(bac=500), "bac", 500)
    assert not reached_threshold(Money(bac=499), "bac", 500)
    assert not reached_threshold(Money(vang=10**9), "bac", 500)  # sai cột
    assert not reached_threshold(Money(), "bac", 1)  # không đọc được != 0
    assert not reached_threshold(Money(bac=10), "bac", 0)  # ngưỡng 0 = tắt


def test_transfer_amount_keeps_reserve() -> None:
    assert transfer_amount(1000, 200) == 800
    assert transfer_amount(100, 200) == 0


def test_transfer_verified_both_sides_within_tolerance() -> None:
    assert transfer_verified(1000, 200, 50, 850, 800, 0)
    assert transfer_verified(1000, 199, 50, 849, 800, 1)  # phí 1 trong dung sai
    assert not transfer_verified(1000, 199, 50, 849, 800, 0)
    assert not transfer_verified(1000, 200, 50, 50, 800, 0)  # bên nhận không tăng
    assert not transfer_verified(None, 200, 50, 850, 800, 5)  # thiếu số = thất bại


def test_money_fields_from_state_dict() -> None:
    assert money_fields({"vang": 1, "bac": "x", "dong": True}) == Money(vang=1)
    assert money_fields(None) == Money()
