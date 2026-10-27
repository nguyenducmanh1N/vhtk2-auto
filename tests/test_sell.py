"""Tests for the sell task (R3b) — the safety-critical path.

The game sells INSTANTLY on tap (no confirm dialog), so these tests pin the
tool's own protections: whitelist-only, quality gate, dry_run never taps,
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
    best_whitelist_match,
    cell_highlight_fraction,
)

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
REPO_ROOT = Path(__file__).parent.parent

# màu nền highlight "bán được" nằm giữa dải cấu hình (h 35, s 150, v 150)
_HL_BGR = cv2.cvtColor(np.uint8([[[35, 150, 150]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()


def make_icon() -> np.ndarray:
    """A distinctive 36x36 grayscale icon (checker + cross)."""
    icon = np.zeros((36, 36), dtype=np.uint8)
    icon[::4, :] = 255
    icon[:, 16:20] = 200
    return icon


def shop_frame(cells: dict[tuple[int, int], dict[str, object]]) -> np.ndarray:
    """A synthetic 540x960 shop screen.

    ``cells[(row, col)]`` options: highlighted (bool), icon (ndarray|None),
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
        icon = spec.get("icon")
        if icon is not None:
            ic = cast(np.ndarray, icon)
            ih, iw = ic.shape
            oy = (g.cell[1] - ih) // 2
            ox = (g.cell[0] - iw) // 2
            # game tô highlight BÁN-TRONG-SUỐT đè lên icon (đo thực tế: ô có
            # icon vẫn 0.56-0.94 highlight) -> blend thay vì ghi đè
            region = cell[oy : oy + ih, ox : ox + iw].astype(np.float32)
            icon3 = cv2.cvtColor(ic, cv2.COLOR_GRAY2BGR).astype(np.float32)
            cell[oy : oy + ih, ox : ox + iw] = (0.45 * icon3 + 0.55 * region).astype(np.uint8)
    return frame


class FakeTemplate:
    def __init__(self, gray: np.ndarray) -> None:
        self.gray = gray


class SellStore:
    """Template store fake: names + get(name).gray + membership."""

    def __init__(self, names: set[str], icons: dict[str, np.ndarray] | None = None) -> None:
        self._names = set(names)
        self._icons = icons or {}

    @property
    def names(self) -> list[str]:
        return sorted(self._names | set(self._icons))

    def __contains__(self, name: str) -> bool:
        return name in self._names or name in self._icons

    def get(self, name: str) -> FakeTemplate:
        return FakeTemplate(self._icons[name])


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


def test_whitelist_matches_known_icon() -> None:
    icon = make_icon()
    cell = np.zeros((48, 50), dtype=np.uint8)
    cell[6:42, 7:43] = icon
    assert best_whitelist_match(cell, {"sell/ok_test": icon}, 0.85) == "sell/ok_test"


def test_whitelist_rejects_unknown_icon() -> None:
    """Đồ lạ không khớp icon nào -> None -> KHÔNG BÁN (nguyên tắc whitelist)."""
    stranger = np.full((48, 50), 90, dtype=np.uint8)
    cv2.circle(stranger, (25, 24), 14, 230, -1)
    assert best_whitelist_match(stranger, {"sell/ok_test": make_icon()}, 0.85) is None


def test_whitelist_empty_sells_nothing() -> None:
    cell = np.zeros((48, 50), dtype=np.uint8)
    assert best_whitelist_match(cell, {}, 0.85) is None


def test_highlight_fraction() -> None:
    settings = load_settings(SETTINGS_PATH)
    lit = np.full((48, 50, 3), _HL_BGR, dtype=np.uint8)
    dark = np.zeros((48, 50, 3), dtype=np.uint8)
    assert cell_highlight_fraction(lit, settings.sell) > 0.9
    assert cell_highlight_fraction(dark, settings.sell) < 0.05


# ---------------------------------------------------------------------------
# _scan_page — the guarded sell loop
# ---------------------------------------------------------------------------


def scan_once(
    tmp_path: Path,
    cells: dict[tuple[int, int], dict[str, object]],
    icons: dict[str, np.ndarray],
    dry_run: bool = True,
    confirm_mode: bool = True,
) -> tuple[SellTask, TapRecorder]:
    store = SellStore(ALL_SELL_TEMPLATES, icons)
    vision = SellVision(store, shop_frame(cells))
    device = TapRecorder()
    task = SellTask(make_ctx(tmp_path, vision, device, dry_run, confirm_mode))
    task._scan_page(0, {n: t for n, t in ((n, store.get(n).gray) for n in icons)})
    return task, device


def test_dry_run_counts_but_never_taps(tmp_path: Path) -> None:
    icon = make_icon()
    cells = {(0, 0): {"icon": icon}, (0, 1): {"icon": icon}}
    task, device = scan_once(tmp_path, cells, {"sell/ok_test": icon})
    assert task.report.would_sell == 2
    assert task.report.sold == 0
    assert device.taps == []  # dry_run: tuyệt đối không tap ô nào


def test_unknown_items_are_kept(tmp_path: Path) -> None:
    stranger = np.full((36, 36), 90, dtype=np.uint8)
    cv2.circle(stranger, (18, 18), 14, 230, -1)
    cells = {(1, 2): {"icon": stranger}}
    task, device = scan_once(tmp_path, cells, {"sell/ok_test": make_icon()})
    assert task.report.skipped_unknown == 1
    assert task.report.would_sell == 0
    assert device.taps == []


def test_quality_items_are_kept(tmp_path: Path) -> None:
    """Icon rác nhưng viền màu lam (phẩm chất >= ngưỡng) -> giữ lại."""
    icon = make_icon()
    # BGR của hue ~110 (lam trong dải cấu hình)
    blue = cv2.cvtColor(np.uint8([[[110, 200, 200]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
    cells = {(0, 0): {"icon": icon, "border_bgr": tuple(blue)}}
    task, device = scan_once(tmp_path, cells, {"sell/ok_test": icon})
    assert task.report.skipped_quality == 1
    assert device.taps == []


def test_real_sale_taps_whitelisted_cells_only(tmp_path: Path) -> None:
    icon = make_icon()
    stranger = np.full((36, 36), 90, dtype=np.uint8)
    cv2.circle(stranger, (18, 18), 14, 230, -1)
    cells = {
        (0, 0): {"icon": icon},  # bán
        (0, 1): {"icon": stranger},  # giữ (không khớp whitelist)
        (0, 2): {"icon": icon, "highlighted": False},  # game không tô -> bỏ qua
    }
    task, device = scan_once(
        tmp_path, cells, {"sell/ok_test": icon}, dry_run=False, confirm_mode=False
    )
    assert task.report.sold == 1
    assert task.report.skipped_unknown == 1
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
