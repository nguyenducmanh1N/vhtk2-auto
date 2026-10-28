"""Tests for the farm phase (R3): monitor policy, map slug, HP color read,
map navigation and the farm/retreat state machines.

Device-free: a small "world" fake answers vision by template presence and
lets taps mutate that presence (e.g. tapping the minimap opens the map).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import cv2
import numpy as np
import pytest

from vhtk.config import (
    AccountConfig,
    FarmSkillSettings,
    HSVRange,
    Settings,
    load_coordinates,
    load_settings,
)
from vhtk.core.device import Device
from vhtk.core.errors import StateTimeout, TaskDefinitionError, TravelStalled, VerifyFailed
from vhtk.core.fsm import TaskContext, TaskStatus
from vhtk.core.recovery import Recovery, RecoveryOutcome
from vhtk.core.safety import Safety
from vhtk.core.vision import Match, Vision, VisionError, bar_fill_percent
from vhtk.mapstore import MapStore
from vhtk.notify import NullNotifier
from vhtk.tasks import navigate as navigate_mod
from vhtk.tasks.farm import (
    TPL_AUTO_OFF,
    TPL_AUTO_ON,
    TPL_HUD,
    FarmSignal,
    FarmTask,
    MonitorAction,
    MonitorPolicy,
    MonitorReading,
    SkillCaster,
)
from vhtk.tasks.navigate import (
    TPL_BTN_CLOSE_MAP,
    TPL_BTN_CONFIRM_COORDS,
    TPL_BTN_SIT,
    TPL_BTN_STAND,
    TPL_TAB_FIND_MAP_OFF,
    TPL_TAB_FIND_MAP_ON,
    TPL_TRAVEL_INDICATOR,
    Navigator,
    map_slug,
)
from vhtk.tasks.retreat import RetreatTask

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
REPO_ROOT = Path(__file__).parent.parent

FARM_MAP = "Long Tuyền Thôn"
TOWN_MAP = "Tuyền Châu"
ALL_TEMPLATES = {
    TPL_HUD,
    TPL_BTN_CONFIRM_COORDS,
    TPL_BTN_CLOSE_MAP,
    TPL_TAB_FIND_MAP_ON,
    TPL_TAB_FIND_MAP_OFF,
    TPL_AUTO_ON,
    TPL_AUTO_OFF,
    TPL_TRAVEL_INDICATOR,
    TPL_BTN_STAND,
    TPL_BTN_SIT,
    "map/hud_name_long_tuyen_thon",
    "map/row_long_tuyen_thon",
    "map/hud_name_tuyen_chau",
    "map/row_tuyen_chau",
}

RED = HSVRange(h=(172, 8), s=(120, 255), v=(90, 255))


def frame_with_hp(fill: float) -> np.ndarray:
    """A 540x960 black frame whose HP bar region is ``fill`` fraction red."""
    frame = np.zeros((540, 960, 3), dtype=np.uint8)
    x, y, w, h = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").farm.hp_bar_rect
    filled = int(w * fill)
    if filled:
        frame[y : y + h, x : x + filled] = (40, 40, 220)  # BGR đỏ
    return frame


class WorldVision:
    """Vision fake: a template "matches" iff its name is in ``present``."""

    def __init__(self, present: set[str], store_names: set[str] | None = None,
                 frame: np.ndarray | None = None,
                 frames: list[np.ndarray] | None = None) -> None:
        self.present = present
        self.store = store_names if store_names is not None else set(ALL_TEMPLATES)
        self.frame = frame if frame is not None else frame_with_hp(1.0)
        # frames: chuỗi khung trả lần lượt cho capture_screen; hết thì giữ khung cuối
        self.frames = list(frames) if frames else []

    def _match(self, name: str) -> Match | None:
        return Match(bbox=(10, 10, 20, 20), score=0.99) if name in self.present else None

    def find(self, name: str, screenshot: object = None, threshold: object = None) -> Match | None:
        return self._match(name)

    def locate(self, name: str, screenshot: object = None) -> Match:
        score = 0.99 if name in self.present else 0.30
        return Match(bbox=(10, 10, 20, 20), score=score)

    def threshold_for(self, name: str, override: float | None = None) -> float:
        return 0.85

    def wait_for(self, name: str, timeout: float | None = None,
                 interval: float | None = None) -> Match | None:
        return self._match(name)

    def wait_any(self, names: Sequence[str], timeout: float | None = None,
                 interval: float | None = None) -> tuple[str, Match] | None:
        for name in names:
            m = self._match(name)
            if m is not None:
                return name, m
        return None

    def wait_vanish(self, name: str, timeout: float | None = None,
                    interval: float | None = None) -> bool:
        return name not in self.present

    def capture_screen(self) -> np.ndarray:
        if self.frames:
            self.frame = self.frames.pop(0)
        return self.frame


class WorldDevice:
    """Device fake; a test may hook taps to mutate the world."""

    def __init__(self, on_tap: Callable[[int, int], None] | None = None) -> None:
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.texts: list[str] = []
        self.deletes: list[int] = []
        self.backs = 0
        self._on_tap = on_tap

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        if self._on_tap:
            self._on_tap(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.swipes.append((x1, y1, x2, y2))

    def input_text(self, text: str) -> None:
        self.texts.append(text)

    def press_delete(self, count: int) -> None:
        self.deletes.append(count)

    def press_back(self) -> None:
        self.backs += 1

    def app_in_foreground(self, package: str) -> bool:
        return True

    def start_app(self, package: str) -> None:  # pragma: no cover
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
            "farm": {
                "map": FARM_MAP,
                "spot": [200, 200],
                "hp_retreat_percent": 30,
                "hp_resume_percent": 90,
                "retreat_map": TOWN_MAP,
                "retreat_spot": [180, 175],
                "gold_transfer_threshold": 1,
            },
        }
    )


def fast_settings() -> Settings:
    settings = load_settings(SETTINGS_PATH)
    farm = settings.farm.model_copy(
        update={
            "monitor_interval": 0.001,
            "arrive_check_interval": 0.001,
            "arrive_stable_cycles": 2,
            "travel_timeout": 2.0,
            "travel_start_timeout": 0.05,
            "travel_stall_cycles": 1000,  # mặc định: test không kích hoạt stall
            "rest_timeout": 1.0,
            "map_timeout": 0.2,
            "hp_low_cycles": 3,
            "engage_settle_seconds": 0.0,
            "engage_timeout": 0.05,
        }
    )
    paths = settings.paths.model_copy(
        update={"coordinates_file": REPO_ROOT / "config" / "coordinates.yaml"}
    )
    safety = settings.safety.model_copy(update={"max_recover_attempts": 1})
    return settings.model_copy(update={"farm": farm, "paths": paths, "safety": safety})


def make_ctx(tmp_path: Path, vision: WorldVision, device: WorldDevice,
             account: AccountConfig | None = None) -> TaskContext:
    settings = fast_settings()
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
        account=account if account is not None else make_account(),
        log_dir=tmp_path / "logs",
    )


# ---------------------------------------------------------------------------
# MonitorPolicy — pure decision logic
# ---------------------------------------------------------------------------


def make_policy(**kw: int) -> MonitorPolicy:
    defaults = {"hp_retreat_percent": 30, "hp_emergency_percent": 15,
                "hp_low_cycles": 3, "lost_cycles": 3, "auto_reenable_limit": 2}
    defaults.update(kw)
    return MonitorPolicy(**defaults)  # type: ignore[arg-type]


def reading(hp: float | None = 100.0, auto: bool | None = True,
            in_map: bool | None = True, moving: bool | None = None,
            bag_full: bool | None = None) -> MonitorReading:
    return MonitorReading(hp_percent=hp, auto_on=auto, in_farm_map=in_map,
                          moving=moving, bag_full=bag_full)


def test_policy_hp_low_needs_consecutive_cycles() -> None:
    p = make_policy()
    assert p.evaluate(reading(hp=20)) is MonitorAction.KEEP_FARMING
    assert p.evaluate(reading(hp=20)) is MonitorAction.KEEP_FARMING
    assert p.evaluate(reading(hp=20)) is MonitorAction.SIGNAL_HP_LOW


def test_policy_hp_emergency_signals_immediately() -> None:
    """Dưới ngưỡng khẩn cấp thì rút NGAY — không chờ đủ chu kỳ xác nhận."""
    p = make_policy()
    assert p.evaluate(reading(hp=10)) is MonitorAction.SIGNAL_HP_LOW


def test_policy_hp_recovery_resets_streak() -> None:
    p = make_policy()
    p.evaluate(reading(hp=20))
    p.evaluate(reading(hp=20))
    assert p.evaluate(reading(hp=80)) is MonitorAction.KEEP_FARMING  # auto hồi/uống thuốc
    p.evaluate(reading(hp=20))
    assert p.evaluate(reading(hp=20)) is MonitorAction.KEEP_FARMING  # streak đếm lại từ đầu


def test_policy_unreadable_frames_then_lost() -> None:
    p = make_policy()
    blind = reading(hp=None, auto=None, in_map=None)
    assert p.evaluate(blind) is MonitorAction.KEEP_FARMING
    assert p.evaluate(blind) is MonitorAction.KEEP_FARMING
    assert p.evaluate(blind) is MonitorAction.LOST


def test_policy_reenables_auto_up_to_limit() -> None:
    p = make_policy(auto_reenable_limit=2)
    assert p.evaluate(reading(auto=False)) is MonitorAction.REENABLE_AUTO
    assert p.evaluate(reading(auto=False)) is MonitorAction.REENABLE_AUTO
    # bật mãi không dính -> có gì đó sai, để RECOVER xử lý
    assert p.evaluate(reading(auto=False)) is MonitorAction.LOST


def test_policy_hp_low_wins_over_auto_and_map() -> None:
    """Máu thấp phải thắng mọi tín hiệu khác — an toàn nhân vật trước."""
    p = make_policy(hp_low_cycles=1)
    assert p.evaluate(reading(hp=5, auto=False, in_map=False)) is MonitorAction.SIGNAL_HP_LOW


def test_policy_wrong_map() -> None:
    p = make_policy()
    assert p.evaluate(reading(in_map=False)) is MonitorAction.WRONG_MAP


def test_policy_bag_full_needs_consecutive_cycles() -> None:
    p = make_policy(bag_full_cycles=2)
    assert p.evaluate(reading(bag_full=True)) is MonitorAction.KEEP_FARMING
    assert p.evaluate(reading(bag_full=True)) is MonitorAction.SIGNAL_BAG_FULL


def test_policy_bag_full_streak_resets_on_not_full() -> None:
    p = make_policy(bag_full_cycles=2)
    p.evaluate(reading(bag_full=True))
    assert p.evaluate(reading(bag_full=False)) is MonitorAction.KEEP_FARMING
    # một chu kỳ không kiểm được (None) không reset cũng không tăng streak
    assert p.evaluate(reading(bag_full=True)) is MonitorAction.KEEP_FARMING
    assert p.evaluate(reading(bag_full=None)) is MonitorAction.KEEP_FARMING
    assert p.evaluate(reading(bag_full=True)) is MonitorAction.SIGNAL_BAG_FULL


def test_policy_hp_low_wins_over_bag_full() -> None:
    """An toàn nhân vật trước: máu thấp thắng túi đầy."""
    p = make_policy(hp_low_cycles=1, bag_full_cycles=1)
    assert p.evaluate(reading(hp=5, bag_full=True)) is MonitorAction.SIGNAL_HP_LOW


def test_policy_bag_full_wins_over_wrong_map() -> None:
    """Túi đã đầy thì quay lại đúng map farm cũng vô ích — về bán trước."""
    p = make_policy(bag_full_cycles=1)
    assert p.evaluate(reading(in_map=False, bag_full=True)) is MonitorAction.SIGNAL_BAG_FULL


def test_policy_wakes_dormant_auto_after_idle_cycles() -> None:
    """Nút auto ON nhưng đứng ngây đủ chu kỳ -> tắt/bật lại để đánh thức AI."""
    p = make_policy(idle_cycles=3)
    assert p.evaluate(reading(moving=False)) is MonitorAction.KEEP_FARMING
    assert p.evaluate(reading(moving=False)) is MonitorAction.KEEP_FARMING
    assert p.evaluate(reading(moving=False)) is MonitorAction.WAKE_AUTO
    # sau khi đánh thức, streak đếm lại từ đầu
    assert p.evaluate(reading(moving=False)) is MonitorAction.KEEP_FARMING


def test_policy_movement_resets_idle_streak() -> None:
    p = make_policy(idle_cycles=2)
    p.evaluate(reading(moving=False))
    assert p.evaluate(reading(moving=True)) is MonitorAction.KEEP_FARMING  # đang săn quái
    assert p.evaluate(reading(moving=False)) is MonitorAction.KEEP_FARMING  # đếm lại
    assert p.evaluate(reading(moving=False)) is MonitorAction.WAKE_AUTO


def test_policy_wake_auto_shares_reenable_quota() -> None:
    """Giật nút mãi không tỉnh -> LOST để RECOVER xử lý, không giật vô hạn."""
    p = make_policy(idle_cycles=1, auto_reenable_limit=2)
    assert p.evaluate(reading(moving=False)) is MonitorAction.WAKE_AUTO
    assert p.evaluate(reading(moving=False)) is MonitorAction.WAKE_AUTO
    assert p.evaluate(reading(moving=False)) is MonitorAction.LOST


# ---------------------------------------------------------------------------
# map_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Long Tuyền Thôn", "long_tuyen_thon"),
        ("Tuyền Châu", "tuyen_chau"),
        ("Thanh Âm động_1", "thanh_am_dong_1"),
        ("Đào Hoa đảo", "dao_hoa_dao"),
        ("  Vũ   Di sơn  ", "vu_di_son"),
    ],
)
def test_map_slug(name: str, slug: str) -> None:
    assert map_slug(name) == slug


# ---------------------------------------------------------------------------
# bar_fill_percent
# ---------------------------------------------------------------------------


def test_bar_fill_full_half_empty() -> None:
    for fill, lo, hi in ((1.0, 95, 100), (0.5, 40, 60), (0.0, 0, 5)):
        frame = frame_with_hp(fill)
        x, y, w, h = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").farm.hp_bar_rect
        pct = bar_fill_percent(frame, (x, y, w, h), RED)
        assert lo <= pct <= hi, f"fill={fill} -> {pct}"


def test_bar_fill_wrapped_hue_matches_both_red_ends() -> None:
    frame = np.zeros((10, 100, 3), dtype=np.uint8)
    frame[:, :50] = (30, 30, 200)  # đỏ phía hue ~179
    frame[:, 50:80] = (60, 50, 230)  # đỏ phía hue ~5
    pct = bar_fill_percent(frame, (0, 0, 100, 10), RED)
    assert 75 <= pct <= 85


def test_bar_fill_bad_rect_raises() -> None:
    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    with pytest.raises(VisionError):
        bar_fill_percent(frame, (5, 5, 20, 20), RED)


# ---------------------------------------------------------------------------
# Navigator
# ---------------------------------------------------------------------------


def nav_for(tmp_path: Path, vision: WorldVision, device: WorldDevice) -> Navigator:
    ctx = make_ctx(tmp_path, vision, device)
    coords = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml")
    return Navigator(ctx, coords.farm, ctx.settings.farm)


def test_open_map_taps_minimap_and_verifies(tmp_path: Path) -> None:
    vision = WorldVision(present=set())
    device = WorldDevice(on_tap=lambda x, y: vision.present.add(TPL_BTN_CONFIRM_COORDS))
    nav = nav_for(tmp_path, vision, device)
    nav.open_map()
    coords = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").farm
    assert device.taps == [coords.minimap]


def test_open_map_timeout_raises(tmp_path: Path) -> None:
    vision = WorldVision(present=set())
    nav = nav_for(tmp_path, vision, WorldDevice())
    with pytest.raises(StateTimeout):
        nav.open_map()


def test_select_map_switches_tab_then_taps_row(tmp_path: Path) -> None:
    # dòng map CHƯA hiện (đang ở tab khác) — phải tap tab rồi mới thấy dòng
    vision = WorldVision(present={TPL_TAB_FIND_MAP_OFF})

    def on_tap(x: int, y: int) -> None:
        vision.present.add("map/row_long_tuyen_thon")

    device = WorldDevice(on_tap=on_tap)
    nav = nav_for(tmp_path, vision, device)
    nav.select_map(FARM_MAP)
    assert len(device.taps) == 2  # tab + dòng map


def test_select_map_row_visible_skips_tab_dance(tmp_path: Path) -> None:
    # ticker của game che hàng tab (không template tab nào match) nhưng dòng
    # map đã hiện — tap thẳng dòng, không đụng tới tab
    vision = WorldVision(present={"map/row_long_tuyen_thon"})
    device = WorldDevice()
    nav = nav_for(tmp_path, vision, device)
    nav.select_map(FARM_MAP)
    assert len(device.taps) == 1  # chỉ dòng map


def test_select_map_missing_row_template_raises(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_TAB_FIND_MAP_ON}, store_names={TPL_TAB_FIND_MAP_ON,
                                                                     TPL_TAB_FIND_MAP_OFF})
    nav = nav_for(tmp_path, vision, WorldDevice())
    with pytest.raises(VerifyFailed, match="not in the map store"):
        nav.select_map(FARM_MAP)


# ---------------------------------------------------------------------------
# Navigator qua KHO map (map không có template row_/hud_name_)
# ---------------------------------------------------------------------------

STORE_MAP = "Dã Trư lâm"  # cố ý không có template nào trong ALL_TEMPLATES
COORDS = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").farm


def _pattern(seed: int, h: int, w: int) -> np.ndarray:
    """Deterministic bright-blocks image — ảnh "chữ" tổng hợp để match.

    Vị trí/kích thước block lấy từ RNG theo seed để hai seed khác nhau không
    thể khớp kể cả khi matchTemplate trượt ngang (block cách đều từng khớp giả).
    """
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 30, dtype=np.uint8)
    for _ in range(8):
        x = int(rng.integers(0, w - 10))
        y0 = int(rng.integers(1, max(2, h // 2)))
        y1 = int(rng.integers(h // 2 + 1, h - 1))
        img[y0:y1, x : x + int(rng.integers(3, 10))] = 255
    return img


def build_map_store(tmp_path: Path, title_seed: int = 2) -> MapStore:
    import yaml as pyyaml

    root = tmp_path / "maps"
    root.mkdir(exist_ok=True)
    _, _, _, th = COORDS.map_title_rect or (0, 0, 0, 26)
    images = {
        "000_map.png": _pattern(9, 50, 50),
        "000_row.png": _pattern(1, 24, 180),
        "000_title.png": _pattern(title_seed, th, 200),
    }
    for fname, img in images.items():
        cv2.imwrite(str(root / fname), img)
    manifest = {"maps": [{"index": 0, "name": STORE_MAP, "page": 0,
                          "files": {"map": "000_map.png", "row": "000_row.png",
                                    "title": "000_title.png"}}]}
    (root / "manifest.yaml").write_text(
        pyyaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8"
    )
    return MapStore(root)


def store_frame(*, row: bool = False, title: bool = False,
                title_seed: int = 2) -> np.ndarray:
    """A HUD-black frame with the store row/title pattern pasted in place."""
    frame = np.zeros((540, 960, 3), dtype=np.uint8)
    if row:
        lx, ly, _, _ = (COORDS.map_list.rect if COORDS.map_list else (95, 140, 215, 350))
        frame[ly + 60 : ly + 84, lx + 5 : lx + 185] = _pattern(1, 24, 180)
    if title:
        tx, ty, _, th = COORDS.map_title_rect or (360, 34, 240, 26)
        frame[ty : ty + th, tx : tx + 200] = _pattern(title_seed, th, 200)
    return frame


@pytest.fixture()
def no_list_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(navigate_mod, "LIST_TAB_SETTLE", 0.0)
    monkeypatch.setattr(navigate_mod, "LIST_SCROLL_SETTLE", 0.0)
    monkeypatch.setattr(navigate_mod, "LIST_PAGE_SETTLE", 0.0)


def nav_with_store(tmp_path: Path, vision: WorldVision, device: WorldDevice,
                   title_seed: int = 2) -> Navigator:
    ctx = make_ctx(tmp_path, vision, device)
    farm_cfg = ctx.settings.farm.model_copy(update={"map_list_max_pages": 2})
    return Navigator(ctx, COORDS, farm_cfg, store=build_map_store(tmp_path, title_seed))


def in_list_rect(xy: tuple[int, int]) -> bool:
    assert COORDS.map_list is not None
    lx, ly, lw, lh = COORDS.map_list.rect
    return lx <= xy[0] <= lx + lw and ly <= xy[1] <= ly + lh


def test_select_map_from_store_taps_row_and_verifies_title(
    tmp_path: Path, no_list_settle: None
) -> None:
    vision = WorldVision(present={TPL_TAB_FIND_MAP_ON}, frame=store_frame(row=True))

    def on_tap(x: int, y: int) -> None:
        # tap trúng dòng -> game preview map, tiêu đề đổi theo
        vision.frame = store_frame(row=True, title=True)

    device = WorldDevice(on_tap=on_tap)
    nav = nav_with_store(tmp_path, vision, device)
    nav.select_map(STORE_MAP)
    assert len(device.taps) == 1 and in_list_rect(device.taps[0])
    assert device.swipes == []  # dòng hiện sẵn trên trang — không phải cuộn


def test_select_map_from_store_scrolls_until_row_found(
    tmp_path: Path, no_list_settle: None
) -> None:
    # trang đang mở KHÔNG có dòng -> kéo về đầu rồi dò trang; trang thứ 2 có
    vision = WorldVision(
        present={TPL_TAB_FIND_MAP_ON},
        frames=[store_frame(), store_frame(), store_frame(row=True)],
    )

    def on_tap(x: int, y: int) -> None:
        vision.frame = store_frame(row=True, title=True)
        vision.frames = []

    device = WorldDevice(on_tap=on_tap)
    nav = nav_with_store(tmp_path, vision, device)
    nav.select_map(STORE_MAP)
    assert device.swipes  # đã phải cuộn
    assert len(device.taps) == 1 and in_list_rect(device.taps[0])


def test_select_map_from_store_row_never_found_times_out(
    tmp_path: Path, no_list_settle: None
) -> None:
    vision = WorldVision(present={TPL_TAB_FIND_MAP_ON}, frame=store_frame())
    nav = nav_with_store(tmp_path, vision, WorldDevice())
    with pytest.raises(StateTimeout, match="not found in the map list"):
        nav.select_map(STORE_MAP)


def test_select_map_from_store_wrong_title_keeps_scanning(
    tmp_path: Path, no_list_settle: None
) -> None:
    """Tap trúng một dòng nhưng tiêu đề preview không khớp map muốn chọn
    (dòng tên gần giống) — phải cuộn tìm tiếp chứ không nhận bừa; hết danh
    sách vẫn không có thì StateTimeout, tuyệt đối không farm nhầm map."""
    vision = WorldVision(present={TPL_TAB_FIND_MAP_ON}, frame=store_frame(row=True))

    def on_tap(x: int, y: int) -> None:
        vision.frame = store_frame(row=True, title=True, title_seed=5)  # tiêu đề map KHÁC

    device = WorldDevice(on_tap=on_tap)
    nav = nav_with_store(tmp_path, vision, device)
    with pytest.raises(StateTimeout, match="not found in the map list"):
        nav.select_map(STORE_MAP)
    assert device.swipes  # đã cuộn tìm tiếp sau cú tap hụt


class TitleWorld:
    """World for confirm_in_map: minimap tap opens the map screen (title =
    CURRENT map — probe AVD 28/10), X button closes it."""

    def __init__(self, vision: WorldVision, title_seed: int) -> None:
        self.vision = vision
        self.title_seed = title_seed

    def on_tap(self, x: int, y: int) -> None:
        if (x, y) == COORDS.minimap:
            self.vision.present.add(TPL_BTN_CONFIRM_COORDS)
            self.vision.frame = store_frame(title=True, title_seed=self.title_seed)
        elif (x, y) == (20, 20):  # tâm Match của nút X trong WorldVision
            self.vision.present.discard(TPL_BTN_CONFIRM_COORDS)
            self.vision.frame = store_frame()


def test_confirm_in_map_matches_title_and_closes_map(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_BTN_CLOSE_MAP}, frame=store_frame())
    world = TitleWorld(vision, title_seed=2)
    nav = nav_with_store(tmp_path, vision, WorldDevice(on_tap=world.on_tap))
    assert nav.confirm_in_map(STORE_MAP) is True
    assert TPL_BTN_CONFIRM_COORDS not in vision.present  # bản đồ đã đóng lại


def test_confirm_in_map_wrong_title_returns_false(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_BTN_CLOSE_MAP}, frame=store_frame())
    world = TitleWorld(vision, title_seed=5)  # đang đứng ở map khác
    nav = nav_with_store(tmp_path, vision, WorldDevice(on_tap=world.on_tap))
    assert nav.confirm_in_map(STORE_MAP) is False
    assert TPL_BTN_CONFIRM_COORDS not in vision.present


def test_in_map_returns_none_without_hud_template(tmp_path: Path) -> None:
    nav = nav_with_store(tmp_path, WorldVision(present=set()), WorldDevice())
    assert nav.in_map(STORE_MAP) is None


def test_wait_arrival_store_map_arrives_via_title(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_BTN_CLOSE_MAP}, frame=store_frame())
    world = TitleWorld(vision, title_seed=2)
    nav = nav_with_store(tmp_path, vision, WorldDevice(on_tap=world.on_tap))
    nav.wait_arrival(STORE_MAP)  # khung đứng yên -> confirm tiêu đề -> arrived


def test_wait_arrival_store_map_title_mismatch_twice_stalls(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_BTN_CLOSE_MAP}, frame=store_frame())
    world = TitleWorld(vision, title_seed=5)  # dừng hẳn nhưng ở map KHÁC
    nav = nav_with_store(tmp_path, vision, WorldDevice(on_tap=world.on_tap))
    with pytest.raises(TravelStalled, match="title mismatch twice"):
        nav.wait_arrival(STORE_MAP)


def test_map_requirements_missing_prefers_templates_then_store(tmp_path: Path) -> None:
    # có đủ cặp template -> không thiếu gì, kho không cần
    nav_tpl = nav_for(tmp_path, WorldVision(present=set()), WorldDevice())
    assert nav_tpl.map_requirements_missing(FARM_MAP) == []
    # không template nhưng map có trong kho + đủ toạ độ -> cũng chạy được
    nav_store = nav_with_store(tmp_path, WorldVision(present=set()), WorldDevice())
    assert nav_store.map_requirements_missing(STORE_MAP) == []
    # không template, không kho -> thiếu, kèm gợi ý quét kho
    missing = nav_tpl.map_requirements_missing(STORE_MAP)
    assert missing and all("harvest" in m for m in missing)


def test_resolve_spot_default_is_map_image_center(tmp_path: Path) -> None:
    nav = nav_for(tmp_path, WorldVision(present=set()), WorldDevice())
    x, y, w, h = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").farm.map_image_rect
    assert nav.resolve_spot((0, 0)) == (x + w // 2, y + h // 2)
    assert nav.resolve_spot((590, 270)) == (590, 270)  # đã cấu hình thì giữ nguyên


def test_tap_map_point_starts_travel_and_map_closes(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_BTN_CONFIRM_COORDS})

    def on_tap(x: int, y: int) -> None:
        if (x, y) == (500, 250):
            vision.present.discard(TPL_BTN_CONFIRM_COORDS)  # map tự đóng khi tìm đường

    device = WorldDevice(on_tap=on_tap)
    nav = nav_for(tmp_path, vision, device)
    nav.tap_map_point(500, 250)
    assert device.taps == [(500, 250)]


def test_tap_map_point_map_stays_open_raises(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_BTN_CONFIRM_COORDS})
    nav = nav_for(tmp_path, vision, WorldDevice())
    with pytest.raises(VerifyFailed, match="stayed open"):
        nav.tap_map_point(500, 250)


def test_ensure_standing_stands_up_sitting_character(tmp_path: Path) -> None:
    """Ngồi đả toạ chặn tìm đường — ensure_standing phải đứng dậy trước."""
    vision = WorldVision(present={TPL_BTN_STAND})

    def on_tap(x: int, y: int) -> None:
        vision.present.discard(TPL_BTN_STAND)
        vision.present.add(TPL_BTN_SIT)

    device = WorldDevice(on_tap=on_tap)
    nav = nav_for(tmp_path, vision, device)
    nav.ensure_standing()
    assert len(device.taps) == 1

    nav.ensure_standing()  # đã đứng -> không tap thêm
    assert len(device.taps) == 1


def test_auto_state_ambiguous_scores_mean_unknown(tmp_path: Path) -> None:
    """Hai điểm match quá sát nhau -> không kết luận, không tap bừa."""
    vision = WorldVision(present={TPL_AUTO_ON, TPL_AUTO_OFF})  # cả hai 0.99 -> diff 0
    nav = nav_for(tmp_path, vision, WorldDevice())
    assert nav.auto_state() == (None, None)


def test_wait_movement_static_frame_returns_false(tmp_path: Path) -> None:
    vision = WorldVision(present=set())
    nav = nav_for(tmp_path, vision, WorldDevice())
    assert nav.wait_movement(0.05) is False


def test_wait_arrival_detects_still_coords(tmp_path: Path) -> None:
    vision = WorldVision(present={"map/hud_name_long_tuyen_thon"})
    nav = nav_for(tmp_path, vision, WorldDevice())
    t0 = time.monotonic()
    nav.wait_arrival(FARM_MAP)  # frame bất động -> ổn định ngay sau 2 chu kỳ
    assert time.monotonic() - t0 < 1.0


def test_wait_arrival_stalled_when_wrong_map(tmp_path: Path) -> None:
    # đứng yên mãi ngoài map đích = KẸT giữa đường (TravelStalled), caller
    # điều hướng lại — không còn chờ hết travel_timeout như trước
    vision = WorldVision(present=set())  # không bao giờ thấy tên map đích
    nav = nav_for(tmp_path, vision, WorldDevice())
    with pytest.raises(TravelStalled):
        nav.wait_arrival(FARM_MAP)


# ---------------------------------------------------------------------------
# FarmTask
# ---------------------------------------------------------------------------


def test_farm_unconfigured_spot_aborts(tmp_path: Path) -> None:
    account = make_account().model_copy(deep=True)
    account.farm.spot = (0, 0)
    vision = WorldVision(present={TPL_HUD})
    device = WorldDevice()
    result = FarmTask(make_ctx(tmp_path, vision, device, account=account)).run()
    assert result.status is TaskStatus.ABORTED
    assert device.taps == []


def test_farm_missing_templates_aborts(tmp_path: Path) -> None:
    vision = WorldVision(present={TPL_HUD}, store_names={TPL_HUD})
    device = WorldDevice()
    result = FarmTask(make_ctx(tmp_path, vision, device)).run()
    assert result.status is TaskStatus.ABORTED
    assert device.taps == []


def test_farm_low_hp_at_start_signals_retreat_first(tmp_path: Path) -> None:
    """Máu đã cạn ngay khi vào task (vd vừa hồi sinh) -> HP_LOW ngay,
    KHÔNG hành quân tới map farm với máu thấp."""
    vision = WorldVision(
        present={TPL_HUD, TPL_AUTO_ON, "map/hud_name_long_tuyen_thon"},
        frame=frame_with_hp(0.1),
    )

    def on_tap(x: int, y: int) -> None:
        # tap nút auto đang bật -> auto tắt
        vision.present.discard(TPL_AUTO_ON)
        vision.present.add(TPL_AUTO_OFF)

    device = WorldDevice(on_tap=on_tap)
    task = FarmTask(make_ctx(tmp_path, vision, device))
    result = task.run()
    assert result.status is TaskStatus.SUCCESS
    assert task.signal is FarmSignal.HP_LOW
    assert len(device.taps) == 1  # đúng một lần: tắt auto trước khi bàn giao


def test_farm_monitor_signals_on_hp_emergency(tmp_path: Path) -> None:
    """Đang farm máu ổn -> MONITOR; máu sập dưới ngưỡng khẩn cấp -> HP_LOW ngay."""
    frames = [frame_with_hp(0.5)] * 3 + [frame_with_hp(0.1)]
    vision = WorldVision(
        present={TPL_HUD, TPL_AUTO_ON, "map/hud_name_long_tuyen_thon"},
        frames=frames,
    )

    def on_tap(x: int, y: int) -> None:
        vision.present.discard(TPL_AUTO_ON)
        vision.present.add(TPL_AUTO_OFF)

    device = WorldDevice(on_tap=on_tap)
    task = FarmTask(make_ctx(tmp_path, vision, device))
    result = task.run()
    assert result.status is TaskStatus.SUCCESS
    assert task.signal is FarmSignal.HP_LOW
    assert len(device.taps) == 1


def test_read_distrusts_hp_when_hud_invisible(tmp_path: Path) -> None:
    """Thanh máu bị che (hiệu ứng lên cấp, màn load) đọc ra 0% GIẢ —
    không có mỏ neo HUD nào nhìn thấy thì coi HP là 'không đọc được'."""
    vision = WorldVision(present=set(), frame=frame_with_hp(0.0))
    task = FarmTask(make_ctx(tmp_path, vision, WorldDevice()))
    reading = task._read(vision.frame)
    assert reading.hp_percent is None

    # có nút auto nhìn thấy -> HP 0 là thật (chết/trọng thương), phải tin
    vision.present.add(TPL_AUTO_ON)
    reading = task._read(vision.frame)
    assert reading.hp_percent == 0.0


def test_farm_requires_account(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, WorldVision(present=set()), WorldDevice())
    ctx.account = None
    with pytest.raises(TaskDefinitionError, match=r"requires ctx\.account"):
        FarmTask(ctx)


# ---------------------------------------------------------------------------
# RetreatTask
# ---------------------------------------------------------------------------


def test_retreat_unconfigured_aborts(tmp_path: Path) -> None:
    account = make_account().model_copy(deep=True)
    account.farm.retreat_map = ""
    vision = WorldVision(present={TPL_HUD})
    device = WorldDevice()
    result = RetreatTask(make_ctx(tmp_path, vision, device, account=account)).run()
    assert result.status is TaskStatus.ABORTED
    assert device.taps == []


def test_rest_detects_being_pulled_away(tmp_path: Path) -> None:
    """Auto-quest của game kéo nhân vật đi thành khác giữa lúc nghỉ ->
    REST phải phát hiện (2 chu kỳ không thấy tên thành) và xin RECOVER."""
    from vhtk.core.errors import TaskError

    # máu chưa hồi đủ + không thấy tên thành trên HUD
    vision = WorldVision(present={TPL_BTN_STAND}, frame=frame_with_hp(0.5))
    task = RetreatTask(make_ctx(tmp_path, vision, WorldDevice()))
    with pytest.raises(TaskError, match="pulled out"):
        task._rest()


def test_rest_resits_when_stood_up(tmp_path: Path) -> None:
    """Nhân vật bị dựng dậy giữa lúc nghỉ (quest auto-route, người chơi khác
    tác động — quan sát 27/10) -> REST phải phát hiện nút "Ngồi" hiện lại và
    ngồi xuống lại thay vì đứng hồi máu rù rì tới hết rest_timeout."""
    from vhtk.core.fsm import DONE

    vision = WorldVision(
        present={TPL_BTN_STAND, "map/hud_name_tuyen_chau"},
        frames=[frame_with_hp(0.5), frame_with_hp(0.5), frame_with_hp(1.0)],
    )
    calls = {"n": 0}
    orig_capture = vision.capture_screen

    def capture() -> np.ndarray:
        calls["n"] += 1
        if calls["n"] == 2:
            # giữa chừng bị dựng dậy: nút Ngồi hiện lại
            vision.present.discard(TPL_BTN_STAND)
            vision.present.add(TPL_BTN_SIT)
        return orig_capture()

    vision.capture_screen = capture  # type: ignore[method-assign]

    def on_tap(x: int, y: int) -> None:
        if TPL_BTN_SIT in vision.present:
            vision.present.discard(TPL_BTN_SIT)
            vision.present.add(TPL_BTN_STAND)

    device = WorldDevice(on_tap=on_tap)
    task = RetreatTask(make_ctx(tmp_path, vision, device))
    assert task._rest() == DONE
    assert len(device.taps) == 1  # đúng một cú tap: ngồi lại


def test_retreat_full_cycle_rests_until_hp_recovers(tmp_path: Path) -> None:
    """HUD -> tắt auto -> mở map -> đã đúng map thành -> tap điểm nghỉ -> nghỉ -> DONE."""
    vision = WorldVision(
        present={TPL_HUD, TPL_AUTO_ON, TPL_BTN_SIT, "map/hud_name_tuyen_chau"},
        frame=frame_with_hp(1.0),  # trong test coi như máu đã hồi sẵn
    )
    coords = load_coordinates(REPO_ROOT / "config" / "coordinates.yaml").farm
    auto_center = (20, 20)
    retreat_spot = (180, 175)  # khớp make_account().farm.retreat_spot

    def on_tap(x: int, y: int) -> None:
        if (x, y) == auto_center:
            # các nút template đều match tại center (20,20) — phân biệt theo
            # trạng thái thế giới: tắt auto trước, rồi mới tới lượt ngồi nghỉ
            if TPL_AUTO_ON in vision.present:
                vision.present.discard(TPL_AUTO_ON)
                vision.present.add(TPL_AUTO_OFF)
            elif TPL_BTN_SIT in vision.present:
                vision.present.discard(TPL_BTN_SIT)
                vision.present.add(TPL_BTN_STAND)
        elif (x, y) == coords.minimap:
            vision.present.add(TPL_BTN_CONFIRM_COORDS)
        elif (x, y) == retreat_spot:
            vision.present.discard(TPL_BTN_CONFIRM_COORDS)  # tìm đường bắt đầu -> map đóng
            # nhân vật "nhúc nhích": khung kế tiếp giữ nguyên, các khung sau
            # đảo pixel vùng toạ độ HUD -> travel_started thấy thay đổi
            cx, cy, cw, ch = coords.hud_coords_rect
            moved = vision.frame.copy()
            moved[cy : cy + ch, cx : cx + cw] ^= 255
            vision.frames = [vision.frame, moved]  # khung cũ 1 lần, rồi "đã đi"

    device = WorldDevice(on_tap=on_tap)
    result = RetreatTask(make_ctx(tmp_path, vision, device)).run()
    assert result.status is TaskStatus.SUCCESS
    assert device.texts == []  # không gõ gì — di chuyển bằng tap lên ảnh bản đồ
    assert retreat_spot in device.taps
    # REST phải NGỒI xuống nghỉ (hồi máu nhanh) — thế giới giả đã lật sang "Đứng"
    assert TPL_BTN_STAND in vision.present


# ---------------------------------------------------------------------------
# SkillCaster — auto nhấn chiêu (phương án A: xoay vòng) + vật phẩm hồi máu
# ---------------------------------------------------------------------------


def skill_cfg(**overrides: object) -> FarmSkillSettings:
    base: dict[str, object] = {
        "enabled": True,
        "slots": [1, 2, 3],
        "cast_interval": 1.0,
        "heal_slot": 8,
        "heal_hp_percent": 60,
        "heal_cooldown": 5.0,
    }
    base.update(overrides)
    return FarmSkillSettings.model_validate(base)


def test_skill_caster_rotates_slots_in_order() -> None:
    caster = SkillCaster(skill_cfg(heal_slot=0))
    # hp cao -> không heal; mỗi lần đến hạn trả ô kế tiếp, xoay vòng
    assert [caster.next_slot(t, 100.0) for t in (0.0, 1.0, 2.0, 3.0)] == [1, 2, 3, 1]


def test_skill_caster_respects_cast_interval() -> None:
    caster = SkillCaster(skill_cfg(heal_slot=0, cast_interval=2.0))
    assert caster.next_slot(0.0, 100.0) == 1
    assert caster.next_slot(1.0, 100.0) is None  # chưa đến hạn
    assert caster.next_slot(2.0, 100.0) == 2


def test_skill_caster_heal_has_priority_and_cooldown() -> None:
    caster = SkillCaster(skill_cfg())
    # máu thấp: thuốc trước, gọi tiếp trong cùng chu kỳ mới tới chiêu
    assert caster.next_slot(0.0, 30.0) == 8
    assert caster.next_slot(0.0, 30.0) == 1
    # trong cooldown thuốc: máu vẫn thấp cũng không spam thuốc, chỉ ra chiêu
    assert caster.next_slot(1.0, 30.0) == 2
    # hết cooldown: thuốc lại được ưu tiên
    assert caster.next_slot(5.0, 30.0) == 8


def test_skill_caster_no_heal_when_hp_unreadable_or_high() -> None:
    caster = SkillCaster(skill_cfg())
    assert caster.next_slot(0.0, None) == 1    # HP không đọc được -> không dám uống thuốc
    assert caster.next_slot(1.0, 99.0) == 2    # máu cao -> chỉ xoay chiêu


def test_skill_caster_disabled_returns_nothing() -> None:
    caster = SkillCaster(skill_cfg(enabled=False))
    assert caster.next_slot(0.0, 10.0) is None


def test_skill_settings_reject_heal_slot_in_rotation() -> None:
    with pytest.raises(Exception, match="heal_slot"):
        skill_cfg(slots=[1, 8])


def test_skill_settings_reject_out_of_range_slot() -> None:
    with pytest.raises(Exception, match=r"1\.\.8"):
        skill_cfg(slots=[9])


def test_farm_aborts_when_skill_slot_unmeasured(tmp_path: Path) -> None:
    """Bật auto chiêu với ô chưa đo toạ độ (0,0) thì VERIFY_HUD phải ABORT."""
    import dataclasses

    import yaml as pyyaml

    vision = WorldVision(present=set(ALL_TEMPLATES))
    ctx = make_ctx(tmp_path, vision, WorldDevice())
    # coordinates riêng cho test: 8 ô chiêu đều chưa đo
    data = pyyaml.safe_load(Path(ctx.settings.paths.coordinates_file).read_text(encoding="utf-8"))
    data["farm"]["skill_slots"] = [[0, 0]] * 8
    patched = tmp_path / "coords_unmeasured.yaml"
    patched.write_text(pyyaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    settings = ctx.settings.model_copy(
        update={
            "farm": ctx.settings.farm.model_copy(update={"skills": skill_cfg()}),
            "paths": ctx.settings.paths.model_copy(update={"coordinates_file": patched}),
        }
    )
    result = FarmTask(dataclasses.replace(ctx, settings=settings)).run()
    assert result.status is TaskStatus.ABORTED


# ---------------------------------------------------------------------------
# mapcal — quy đổi pixel <-> toạ độ game (mảnh 2 dữ liệu map)
# ---------------------------------------------------------------------------


def test_mapcal_fit_and_roundtrip() -> None:
    from vhtk.mapcal import fit_transform, max_residual

    # map 385px rộng ~ 200 đơn vị toạ độ: sx≈0.52, gốc ảnh tại game-x 60
    pairs = [((400, 110), (63, 40)), ((700, 300), (219, 173)), ((550, 200), (141, 103))]
    t = fit_transform(pairs)
    assert max_residual(t, pairs) <= 1.0
    gx, gy = t.to_game(625, 250)
    px, py = t.to_pixel(gx, gy)
    assert abs(px - 625) <= 2 and abs(py - 250) <= 2


def test_mapcal_rejects_degenerate_samples() -> None:
    from vhtk.mapcal import fit_transform

    with pytest.raises(ValueError, match="2 samples"):
        fit_transform([((400, 110), (63, 40))])
    # hai điểm cùng cột: trục x không fit được độ dốc
    with pytest.raises(ValueError, match="identical"):
        fit_transform([((400, 110), (63, 40)), ((400, 300), (63, 173))])


def test_bag_full_two_tiers_and_latch(tmp_path: Path) -> None:
    """Tầng cảnh báo sớm (dòng chat) phát hiện + latch qua vài chu kỳ;
    tầng khoá cứng cần CẢ template icon lẫn tỉ lệ pixel đỏ."""
    from vhtk.tasks.farm import TPL_BAG_FULL, TPL_BAG_HEAVY

    vision = WorldVision(
        present=set(),
        store_names=ALL_TEMPLATES | {TPL_BAG_FULL, TPL_BAG_HEAVY},
    )
    task = FarmTask(make_ctx(tmp_path, vision, WorldDevice()))
    frame = frame_with_hp(1.0)

    # chưa có gì -> không đầy (icon không khớp, không có pixel đỏ trong ô icon)
    assert task._bag_full(frame) is False

    # thấy dòng cảnh báo sớm một lần -> True, và latch giữ True thêm
    # bag_full_cycles chu kỳ dù dòng chat đã trôi mất
    vision.present.add(TPL_BAG_HEAVY)
    assert task._bag_full(frame) is True
    vision.present.discard(TPL_BAG_HEAVY)
    cycles = task.ctx.settings.farm.bag_full_cycles
    for _ in range(cycles):
        assert task._bag_full(frame) is True
    assert task._bag_full(frame) is False  # latch đã cạn

    # tầng khoá cứng: template khớp nhưng KHÔNG có pixel đỏ -> vẫn False
    vision.present.add(TPL_BAG_FULL)
    assert task._bag_full(frame) is False
