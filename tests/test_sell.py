"""Tests for the sell task (R3b) — the safety-critical path.

The game sells INSTANTLY on tap (no confirm dialog), so these tests pin the
tool's own protections: quantity-label and quality gates, dry_run never taps,
real taps only when both dry_run and confirm_mode are off.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import cv2
import numpy as np
import pytest

from vhtk.config import AccountConfig, Settings, load_coordinates, load_settings
from vhtk.core.device import Device
from vhtk.core.fsm import TaskContext, TaskStatus
from vhtk.core.recovery import Recovery, RecoveryOutcome
from vhtk.core.safety import Safety
from vhtk.core.vision import Vision
from vhtk.notify import NullNotifier
from vhtk.tasks.sell import (
    SellTask,
    cell_highlight_fraction,
    has_quantity_label,
)

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
REPO_ROOT = Path(__file__).parent.parent

# màu nền highlight "bán được" nằm giữa dải cấu hình (h 35, s 150, v 150)
_HL_BGR = cv2.cvtColor(np.uint8([[[35, 150, 150]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()


def shop_frame(cells: dict[tuple[int, int], dict[str, object]]) -> np.ndarray:
    """A synthetic 540x960 shop screen.

    ``cells[(row, col)]`` options: highlighted (bool), quantity_label (bool),
    border_bgr (tuple|None) — border color drives detect_quality.
    """
    coords = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").sell
    g = coords.bag_grid
    frame = np.zeros((540, 960, 3), dtype=np.uint8)
    for (row, col), spec in cells.items():
        x = g.origin[0] + col * g.pitch[0]
        y = g.origin[1] + row * g.pitch[1]
        cell = frame[y : y + g.cell[1], x : x + g.cell[0]]
        if spec.get("highlighted", True):
            cell[:] = _HL_BGR
        border = spec.get("border_bgr")
        if border is not None:
            bgr = cast(tuple[int, int, int], border)
            cell[:3, :] = bgr
            cell[-3:, :] = bgr
            cell[:, :3] = bgr
            cell[:, -3:] = bgr
        if spec.get("quantity_label"):
            x, y, _, _ = coords.quantity_badge_rect
            cv2.rectangle(cell, (x + 1, y + 3), (x + 4, y + 8), (255, 255, 255), -1)
            cv2.rectangle(cell, (x + 8, y + 3), (x + 11, y + 8), (255, 255, 255), -1)
    return frame


class SellStore:
    """Template store fake: names + membership."""

    def __init__(self, names: set[str]) -> None:
        self._names = set(names)

    @property
    def names(self) -> list[str]:
        return sorted(self._names)

    def __contains__(self, name: str) -> bool:
        return name in self._names


class SellVision:
    def __init__(self, store: SellStore, frame: np.ndarray) -> None:
        self.store = store
        self.frame = frame

    def capture_screen(self) -> np.ndarray:
        return self.frame

    def find(self, name: str, screenshot: object = None, threshold: object = None) -> None:
        return None

    def wait_any(self, names: object, timeout: object = None, interval: object = None) -> None:
        return None

    def wait_for(self, name: str, timeout: object = None, interval: object = None) -> None:
        return None

    def wait_vanish(self, name: str, timeout: object = None, interval: object = None) -> bool:
        return True


class TapRecorder:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def press_back(self) -> None:  # pragma: no cover
        pass


class OkRecovery:
    def check(self) -> RecoveryOutcome:
        return RecoveryOutcome.OK

    def on_state_failure(self, state: str, reason: str) -> RecoveryOutcome:
        return RecoveryOutcome.RECOVERED


def make_account() -> AccountConfig:
    return AccountConfig.model_validate(
        {
            "name": "test",
            "role": "farm",
            "ldplayer": {"index": 0, "adb_serial": "emulator-5554"},
            "login": {"server": "S9", "username": "", "password": ""},
            "farm": {"map": "Bắc Tuyền Châu", "gold_transfer_threshold": 1},
        }
    )


def make_ctx(
    tmp_path: Path,
    vision: SellVision,
    device: TapRecorder,
    dry_run: bool = True,
    confirm_mode: bool = True,
) -> TaskContext:
    settings: Settings = load_settings(SETTINGS_PATH)
    safety_cfg = settings.safety.model_copy(
        update={"dry_run": dry_run, "confirm_mode": confirm_mode, "max_recover_attempts": 1}
    )
    paths = settings.paths.model_copy(
        update={"coordinates_file": REPO_ROOT / "config" / "coordinates.yaml"}
    )
    settings = settings.model_copy(update={"safety": safety_cfg, "paths": paths})
    notifier = NullNotifier()
    safety = Safety(
        settings.safety, notifier, tmp_path / "dry_run", kill_switch_path=tmp_path / "STOP"
    )
    return TaskContext(
        device=cast(Device, device),
        vision=cast(Vision, vision),
        safety=safety,
        recovery=cast(Recovery, OkRecovery()),
        notifier=notifier,
        settings=settings,
        account=make_account(),
        log_dir=tmp_path / "logs",
    )


ALL_SELL_TEMPLATES = {
    "shop/cua_hieu_header",
    "shop/btn_ban",
    "shop/btn_close_shop",
    "shop/npc_talk_icon",
    "shop/row_chu_tap_hoa",
    "map/btn_confirm_coords",
    "shop/tab_find_npc_on",
    "shop/tab_find_npc_off",
    "login/hud_auto_btn",
    "map/btn_sit",
    "map/btn_stand",
    "map/btn_auto_on",
    "map/btn_auto_off",
}


# ---------------------------------------------------------------------------
# Pure decision functions
# ---------------------------------------------------------------------------


def test_highlight_fraction() -> None:
    settings = load_settings(SETTINGS_PATH)
    lit = np.full((48, 50, 3), _HL_BGR, dtype=np.uint8)
    dark = np.zeros((48, 50, 3), dtype=np.uint8)
    assert cell_highlight_fraction(lit, settings.sell) > 0.9
    assert cell_highlight_fraction(dark, settings.sell) < 0.05


def test_quantity_label_detects_number_and_ignores_sparse_pixels() -> None:
    settings = load_settings(SETTINGS_PATH)
    coords = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").sell
    cell = np.zeros((48, 50, 3), dtype=np.uint8)
    x, y, _, _ = coords.quantity_badge_rect
    cv2.rectangle(cell, (x + 1, y + 3), (x + 4, y + 8), (255, 255, 255), -1)
    cv2.rectangle(cell, (x + 8, y + 3), (x + 11, y + 8), (255, 255, 255), -1)
    assert has_quantity_label(cell, coords.quantity_badge_rect, settings.sell)

    cell[:] = 0
    cv2.rectangle(cell, (x + 1, y + 3), (x + 2, y + 4), (255, 255, 255), -1)
    assert not has_quantity_label(cell, coords.quantity_badge_rect, settings.sell)


# ---------------------------------------------------------------------------
# _scan_page — the guarded sell loop
# ---------------------------------------------------------------------------


def scan_once(
    tmp_path: Path,
    cells: dict[tuple[int, int], dict[str, object]],
    dry_run: bool = True,
    confirm_mode: bool = True,
) -> tuple[SellTask, TapRecorder]:
    store = SellStore(ALL_SELL_TEMPLATES)
    vision = SellVision(store, shop_frame(cells))
    device = TapRecorder()
    task = SellTask(make_ctx(tmp_path, vision, device, dry_run, confirm_mode))
    task._scan_page(0)
    return task, device


def test_dry_run_counts_but_never_taps(tmp_path: Path) -> None:
    cells = {(0, 0): {}, (0, 1): {}}
    task, device = scan_once(tmp_path, cells)
    assert task.report.would_sell == 2
    assert task.report.sold == 0
    assert device.taps == []  # dry_run: tuyệt đối không tap ô nào


def test_numbered_items_are_kept_including_x1(tmp_path: Path) -> None:
    cells = {(1, 2): {"quantity_label": True}}
    task, device = scan_once(tmp_path, cells)
    assert task.report.skipped_numbered == 1
    assert task.report.would_sell == 0
    assert device.taps == []


def test_quality_items_are_kept(tmp_path: Path) -> None:
    """Unnumbered item with blue quality border -> keep."""
    # BGR của hue ~110 (lam trong dải cấu hình)
    blue = cv2.cvtColor(np.uint8([[[110, 200, 200]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
    cells = {(0, 0): {"border_bgr": tuple(blue)}}
    task, device = scan_once(tmp_path, cells)
    assert task.report.skipped_quality == 1
    assert device.taps == []


def test_real_sale_taps_only_unnumbered_highlighted_cells(tmp_path: Path) -> None:
    cells = {
        (0, 0): {},  # unnumbered and highlighted -> sell
        (0, 1): {"quantity_label": True},  # any count label -> keep
        (0, 2): {"highlighted": False},  # game does not mark as sellable
    }
    task, device = scan_once(
        tmp_path, cells, dry_run=False, confirm_mode=False
    )
    assert task.report.sold == 1
    assert task.report.skipped_numbered == 1
    assert len(device.taps) == 1
    grid = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").sell.bag_grid
    assert device.taps[0] == (
        grid.origin[0] + grid.cell[0] // 2,
        grid.origin[1] + grid.cell[1] // 2,
    )


def test_sell_task_missing_templates_aborts(tmp_path: Path) -> None:
    store = SellStore({"login/hud_auto_btn"})
    vision = SellVision(store, shop_frame({}))
    device = TapRecorder()
    task = SellTask(make_ctx(tmp_path, vision, device))
    result = task.run()
    assert result.status is TaskStatus.ABORTED
    assert device.taps == []


def test_sell_task_requires_account(tmp_path: Path) -> None:
    from vhtk.core.errors import TaskDefinitionError

    store = SellStore(ALL_SELL_TEMPLATES)
    ctx = make_ctx(tmp_path, SellVision(store, shop_frame({})), TapRecorder())
    ctx.account = None
    with pytest.raises(TaskDefinitionError, match=r"requires ctx\.account"):
        SellTask(ctx)
