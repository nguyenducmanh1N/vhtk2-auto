"""R3: travel to the configured farm spot, enable the game's own auto-combat,
and monitor it (PLAN.md 4.2).

Design notes:
- The tool does NOT fight: it turns the game's built-in auto on and watches.
- MONITOR is the single long-running state. Every ``farm.monitor_interval``
  seconds it grabs ONE frame and checks: auto still on (re-enable if not),
  still in the farm map, HP level (color read of the HUD bar — no OCR).
- Per PLAN.md, MONITOR only RAISES SIGNALS (``FarmSignal``) for the runner;
  it never handles them itself. On ``HP_LOW`` the task switches auto off and
  finishes — the runner then runs ``RetreatTask`` and loops back to farm.
- Decision logic lives in ``MonitorPolicy`` (pure, unit-testable): consecutive
  low-HP cycles, unreadable-frame streaks, auto re-enable quota.
- Like login, every entry state routes itself from whatever screen is showing,
  so the task is idempotent (already farming -> straight to MONITOR).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum

import structlog

from vhtk.config import Coordinates, FarmSkillSettings, load_coordinates
from vhtk.core.errors import (
    StateTimeout,
    TaskDefinitionError,
    TaskError,
    TravelStalled,
    VerifyFailed,
)
from vhtk.core.fsm import ABORT, DONE, BaseTask, State, TaskContext
from vhtk.core.vision import Image, bar_fill_percent
from vhtk.tasks.navigate import (
    TPL_AUTO_OFF,
    TPL_AUTO_ON,
    TPL_BTN_CONFIRM_COORDS,
    TPL_BTN_SIT,
    TPL_BTN_STAND,
    TPL_TAB_FIND_MAP_OFF,
    TPL_TAB_FIND_MAP_ON,
    Navigator,
    hud_name_template,
    row_template,
)

log = structlog.get_logger(__name__)

# HUD đã vào thế giới — dùng chung template với login
TPL_HUD = "login/hud_auto_btn"

# State names
S_VERIFY_HUD = "VERIFY_HUD"
S_OPEN_MAP = "OPEN_MAP"
S_SELECT_MAP = "SELECT_MAP"
S_TAP_SPOT = "TAP_SPOT"
S_TRAVEL = "TRAVEL_WAIT"
S_ENABLE_AUTO = "ENABLE_AUTO"
S_MONITOR = "MONITOR"
S_DISABLE_AUTO = "DISABLE_AUTO"


class FarmSignal(Enum):
    """What MONITOR observed; consumed by the runner, never handled here."""

    HP_LOW = "hp_low"
    BAG_FULL = "bag_full"  # chưa phát — cần template túi (R3b)
    GOLD_THRESHOLD = "gold_threshold"  # chưa phát — cần OCR vàng (W3)
    EQUIP_BROKEN = "equip_broken"  # chưa phát — cần template hỏng đồ (R5)


@dataclass(frozen=True)
class MonitorReading:
    """One MONITOR cycle's observations; ``None`` = unreadable this cycle."""

    hp_percent: float | None
    auto_on: bool | None
    in_farm_map: bool | None
    # nhân vật có dịch chuyển so với chu kỳ trước không (pixel-diff vùng toạ
    # độ HUD); None = chu kỳ đầu / không đọc được
    moving: bool | None = None


class MonitorAction(Enum):
    """What the MONITOR state should do next, decided by ``MonitorPolicy``."""

    KEEP_FARMING = "keep_farming"
    REENABLE_AUTO = "reenable_auto"
    WAKE_AUTO = "wake_auto"  # nút auto ON nhưng nhân vật đứng ngây -> tắt/bật lại
    WRONG_MAP = "wrong_map"
    SIGNAL_HP_LOW = "signal_hp_low"
    LOST = "lost"  # nhiều chu kỳ liền không đọc được màn hình -> RECOVER


class MonitorPolicy:
    """Pure decision logic of MONITOR (no device, no vision — unit-testable).

    Tracks consecutive low-HP cycles, consecutive unreadable cycles and the
    auto re-enable quota across one MONITOR session.
    """

    def __init__(self, hp_retreat_percent: int, hp_emergency_percent: int,
                 hp_low_cycles: int, lost_cycles: int, auto_reenable_limit: int,
                 idle_cycles: int = 4) -> None:
        self._hp_threshold = hp_retreat_percent
        self._hp_emergency = hp_emergency_percent
        self._hp_low_cycles = hp_low_cycles
        self._lost_cycles = lost_cycles
        self._reenable_limit = auto_reenable_limit
        self._idle_cycles = idle_cycles
        self._low_streak = 0
        self._lost_streak = 0
        self._reenabled = 0
        self._idle_streak = 0

    def evaluate(self, reading: MonitorReading) -> MonitorAction:
        """Decide the next action from one cycle's readings.

        Priority: unreadable screen > HP (safety of the character) > wrong
        map > auto off — a low HP must win even when auto also dropped.
        """
        if reading.hp_percent is None and reading.auto_on is None:
            self._lost_streak += 1
            if self._lost_streak >= self._lost_cycles:
                return MonitorAction.LOST
            return MonitorAction.KEEP_FARMING
        self._lost_streak = 0

        # dưới ngưỡng khẩn cấp: rút NGAY — quái đánh dồn có thể giết nhân vật
        # nhanh hơn số chu kỳ chờ xác nhận
        if reading.hp_percent is not None and reading.hp_percent < self._hp_emergency:
            return MonitorAction.SIGNAL_HP_LOW

        if reading.hp_percent is not None and reading.hp_percent < self._hp_threshold:
            self._low_streak += 1
            if self._low_streak >= self._hp_low_cycles:
                return MonitorAction.SIGNAL_HP_LOW
        else:
            self._low_streak = 0

        if reading.in_farm_map is False:
            return MonitorAction.WRONG_MAP

        if reading.auto_on is False:
            self._reenabled += 1
            if self._reenabled > self._reenable_limit:
                return MonitorAction.LOST
            return MonitorAction.REENABLE_AUTO

        # AI auto của game hay "ngủ gật": nút vẫn ON nhưng nhân vật đứng
        # ngây nhiều chu kỳ liền (quan sát 26/10: đánh 1 phát rồi đứng) —
        # tắt/bật lại auto như thao tác tay là tỉnh. Dùng chung quota với
        # REENABLE để không giật nút vô hạn.
        if reading.moving is False and reading.auto_on is True:
            self._idle_streak += 1
            if self._idle_streak >= self._idle_cycles:
                self._idle_streak = 0
                self._reenabled += 1
                if self._reenabled > self._reenable_limit:
                    return MonitorAction.LOST
                return MonitorAction.WAKE_AUTO
        elif reading.moving is True:
            self._idle_streak = 0
        return MonitorAction.KEEP_FARMING


class SkillCaster:
    """Pure decision logic of auto skill-casting (no device — unit-testable).

    Phương án A: xoay vòng các ô trong ``slots`` mỗi ``cast_interval`` giây;
    chiêu đang hồi thì cú tap vô hại, game tự bỏ qua. Ô ``heal_slot`` có ưu
    tiên tuyệt đối: HP dưới ``heal_hp_percent`` và đã quá ``heal_cooldown``
    kể từ lần dùng trước thì trả về ô thuốc trước mọi chiêu.
    """

    def __init__(self, cfg: FarmSkillSettings) -> None:
        self._cfg = cfg
        self._idx = 0
        self._last_cast: float | None = None
        self._last_heal: float | None = None

    def next_slot(self, now: float, hp_percent: float | None) -> int | None:
        """The slot (1-8) to tap now, or ``None`` when nothing is due.

        Call repeatedly within one cycle: a heal and a rotation cast may both
        be due — the heal comes out first, the next call yields the skill.
        """
        if not self._cfg.enabled:
            return None
        if (
            self._cfg.heal_slot
            and hp_percent is not None
            and hp_percent < self._cfg.heal_hp_percent
            and (self._last_heal is None or now - self._last_heal >= self._cfg.heal_cooldown)
        ):
            self._last_heal = now
            return self._cfg.heal_slot
        if self._cfg.slots and (
            self._last_cast is None or now - self._last_cast >= self._cfg.cast_interval
        ):
            slot = self._cfg.slots[self._idx % len(self._cfg.slots)]
            self._idx += 1
            self._last_cast = now
            return slot
        return None


class FarmTask(BaseTask):
    """Send the character to the farm spot, enable in-game auto, monitor it.

    After ``run()``, :attr:`signal` carries what MONITOR observed (or ``None``
    when the task ended without a signal — abort, kill-switch, relogin).
    """

    name = "farm"

    def __init__(self, ctx: TaskContext) -> None:
        if ctx.account is None:
            raise TaskDefinitionError("FarmTask requires ctx.account")
        self._farm = ctx.account.farm
        coords: Coordinates = load_coordinates(ctx.settings.paths.coordinates_file)
        self._nav = Navigator(ctx, coords.farm, ctx.settings.farm)
        self._hp_rect = coords.farm.hp_bar_rect
        self._skill_points = coords.farm.skill_slots
        self._nav_attempts = 0
        self.signal: FarmSignal | None = None
        super().__init__(ctx)

    def states(self) -> Sequence[State]:
        cfg = self.ctx.settings.farm
        return [
            State(
                S_VERIFY_HUD,
                self._verify_hud,
                30.0,
                frozenset({S_OPEN_MAP, S_MONITOR, S_DISABLE_AUTO}),
            ),
            State(S_OPEN_MAP, self._open_map, cfg.map_timeout, frozenset({S_SELECT_MAP})),
            State(S_SELECT_MAP, self._select_map, cfg.map_timeout, frozenset({S_TAP_SPOT})),
            State(
                S_TAP_SPOT,
                self._tap_spot,
                # tap (có thể vài lần) + chờ xác nhận tìm đường bắt đầu
                cfg.map_timeout + cfg.travel_start_timeout,
                frozenset({S_TRAVEL, S_OPEN_MAP, S_ENABLE_AUTO}),
            ),
            State(
                S_TRAVEL,
                self._travel_wait,
                cfg.travel_timeout,
                frozenset({S_ENABLE_AUTO, S_OPEN_MAP}),
            ),
            State(
                S_ENABLE_AUTO, self._enable_auto, cfg.map_timeout, frozenset({S_MONITOR})
            ),
            # MONITOR chạy vô hạn tới khi có tín hiệu/lỗi — budget chỉ để log
            State(
                S_MONITOR,
                self._monitor,
                float("inf"),
                frozenset({S_DISABLE_AUTO, S_OPEN_MAP}),
            ),
            State(S_DISABLE_AUTO, self._disable_auto, 15.0, frozenset({DONE})),
        ]

    def initial_state(self) -> str:
        return S_VERIFY_HUD

    def recovery_resume_state(self, failed_state: str) -> str:
        """Resume at VERIFY_HUD: after recovery (popup closed, revived...) the
        screen/position may be anywhere — VERIFY_HUD re-routes itself."""
        return S_VERIFY_HUD

    # -- state handlers ----------------------------------------------------------

    def _verify_hud(self) -> str:
        """Land on the HUD from wherever we are; shortcut to MONITOR if the
        account is already farming (right map + auto on)."""
        self.signal = None  # chạy lại cùng instance (sau relogin) không giữ tín hiệu cũ
        if self._farm.spot == (0, 0) or not self._farm.map:
            log.error(
                "farm spot/map not configured for this account",
                account=self.ctx.account.name if self.ctx.account else "?",
            )
            return ABORT
        bad_slots = self._unmeasured_skill_slots()
        if bad_slots:
            # bật auto chiêu mà ô chưa đo toạ độ = tap mù — từ chối chạy
            log.error(
                "auto-skill enabled but slot coordinates not measured",
                slots=bad_slots,
                hint="do toa do trong GUI tab 'Chieu thuc' hoac coordinates.yaml",
            )
            return ABORT
        missing = self._nav.missing_templates(
            TPL_HUD,
            TPL_BTN_CONFIRM_COORDS,
            TPL_TAB_FIND_MAP_ON,
            TPL_TAB_FIND_MAP_OFF,
            TPL_AUTO_ON,
            TPL_AUTO_OFF,
            hud_name_template(self._farm.map),
            row_template(self._farm.map),
        )
        if missing:
            log.error(
                "farm templates missing — capture them with tools/capture.py",
                missing=missing,
            )
            return ABORT
        # nhiều mỏ neo HUD: thành đông người chơi thì nhãn tên che mất nút Auto
        # (abort thực tế 2026-10-26 13:15) — nhãn Đứng/Ngồi góc trái dưới là
        # neo dự phòng
        hit = self.ctx.vision.wait_any(
            [TPL_HUD, TPL_BTN_SIT, TPL_BTN_STAND, TPL_BTN_CONFIRM_COORDS], timeout=20.0
        )
        if hit is None:
            raise StateTimeout("neither HUD nor map screen is showing")
        if hit[0] == TPL_BTN_CONFIRM_COORDS:
            self._nav.close_map()
        frame = self.ctx.vision.capture_screen()
        hp = self._hp(frame)
        if hp is not None and hp < self._farm.hp_retreat_percent:
            # vừa hồi sinh / vừa thoát trận với máu cạn — hành quân lúc này là
            # chết tiếp giữa đường; phát HP_LOW để runner cho đi nghỉ trước
            log.warning("HP already low before farming, signalling retreat first", hp=round(hp, 1))
            self.signal = FarmSignal.HP_LOW
            return S_DISABLE_AUTO
        if (
            self._nav.in_map(self._farm.map, frame=frame)
            and self._nav.auto_state(frame)[0] is True
        ):
            log.info("already farming, going straight to MONITOR")
            return S_MONITOR
        return S_OPEN_MAP

    def _open_map(self) -> str:
        # nhân vật ngồi (đả toạ) thì game từ chối tìm đường — đứng dậy trước
        self._nav.ensure_standing()
        self._nav.open_map()
        return S_SELECT_MAP

    def _select_map(self) -> str:
        """Pick the farm map in the list unless we are already inside it."""
        if not self._nav.in_map(self._farm.map):
            self._nav.select_map(self._farm.map)
        return S_TAP_SPOT

    def _tap_spot(self) -> str:
        cfg = self.ctx.settings.farm
        x, y = self._nav.resolve_spot(tuple(self._farm.spot))
        try:
            self._nav.tap_map_point(x, y)
        except VerifyFailed:
            # tap điểm đến trùng/sát chỗ đang đứng thì game lờ đi, map cứ
            # mở — đã ở trong map farm thì bật auto đánh tại chỗ luôn
            if self._nav.in_map(self._farm.map):
                log.info("already in farm map, engaging in place")
                self._nav.close_map()
                return S_ENABLE_AUTO
            raise
        # map đóng CHƯA đủ để tin: ngay sau login game có lúc đóng map mà
        # không hề bắt đầu tìm đường (quan sát AVD 26/10) — phải thấy dòng
        # "Đi: ..." hoặc toạ độ HUD đổi; chưa thấy thì làm lại từ bước mở map
        if not self._nav.travel_started(cfg.travel_start_timeout):
            self._nav_attempts += 1
            if self._nav_attempts > cfg.nav_retries:
                # ABORT chứ KHÔNG raise: raise -> RECOVER -> resume lại chuỗi
                # OPEN_MAP, mỗi transition thành công reset quota recover của
                # FSM -> lặp vô hạn (bắt được nhờ test treo 26/10)
                log.error("pathfinding never started, giving up", attempts=self._nav_attempts)
                return ABORT
            # từ lần thử 2: nghi kẹt ở cổng map (game từ chối lệnh xuyên map
            # tại đó) — route tới NPC để rời chỗ kẹt rồi mới điều hướng lại
            if self._nav_attempts >= 2 and cfg.unstick_npc_row:
                self._nav.unstick_via_npc(cfg.unstick_npc_row, cfg.unstick_walk_seconds)
            log.warning("travel did not start, redoing navigation", attempt=self._nav_attempts)
            return S_OPEN_MAP
        self._nav_attempts = 0
        return S_TRAVEL

    def _travel_wait(self) -> str:
        try:
            self._nav.wait_arrival(self._farm.map)
        except TravelStalled:
            # game dừng ở cổng map thay vì vượt sang — tap lại điểm đến từ
            # chỗ đang đứng (gần đích hơn) thay vì chờ hết travel_timeout
            self._nav_attempts += 1
            if self._nav_attempts > self.ctx.settings.farm.nav_retries:
                log.error("travel keeps stalling, giving up", attempts=self._nav_attempts)
                return ABORT
            log.warning("travel stalled, redoing navigation", attempt=self._nav_attempts)
            return S_OPEN_MAP
        self._nav_attempts = 0
        return S_ENABLE_AUTO

    def _enable_auto(self) -> str:
        """Turn auto-combat on AND make sure it actually engaged.

        Observed on the AVD: toggling auto right when the character stops
        moving often leaves the game's AI dormant — the button shows ON but
        the character just stands there, even next to mobs; a manual off/on
        wakes it. So: settle first, enable, then expect MOVEMENT (hunting);
        no movement -> re-toggle, up to ``engage_retries`` times.
        """
        cfg = self.ctx.settings.farm
        time.sleep(cfg.engage_settle_seconds)
        self._nav.set_auto(True)
        if cfg.engage_warmup_toggle_seconds > 0:
            # game lỗi: lần bật đầu nhân vật đánh 1 phát rồi đứng ngây (cú
            # đánh đó đủ tạo chuyển động đánh lừa wait_movement) — chủ động
            # tắt/bật lại ngay như thao tác tay, không chờ phát hiện đứng im
            time.sleep(cfg.engage_warmup_toggle_seconds)
            log.info("warmup toggle: re-cycling auto to wake the game AI")
            self._nav.set_auto(False)
            self._nav.set_auto(True)
        for attempt in range(cfg.engage_retries + 1):
            if self._nav.wait_movement(cfg.engage_timeout):
                log.info("auto-combat engaged", map=self._farm.map)
                return S_MONITOR
            if attempt < cfg.engage_retries:
                log.warning("auto on but character idle, re-toggling")
                self._nav.set_auto(False)
                self._nav.set_auto(True)
        # không thấy di chuyển cũng chưa chắc hỏng (quái có thể đứng sát chân)
        # — vào MONITOR, các kiểm tra chu kỳ sẽ nói tiếp
        log.warning("auto enabled but no movement observed, monitoring anyway")
        return S_MONITOR

    def _monitor(self) -> str:
        """The long-running watch loop; exits only with a signal or trouble."""
        cfg = self.ctx.settings.farm
        policy = MonitorPolicy(
            self._farm.hp_retreat_percent,
            cfg.hp_emergency_percent,
            cfg.hp_low_cycles,
            cfg.lost_cycles,
            cfg.auto_reenable_limit,
            idle_cycles=cfg.monitor_idle_cycles,
        )
        caster = SkillCaster(cfg.skills)
        prev_crop: Image | None = None
        while True:
            self.ctx.safety.check_kill_switch()
            frame = self.ctx.vision.capture_screen()
            reading = self._read(frame)
            # phát hiện đứng ngây: pixel-diff vùng toạ độ HUD giữa 2 chu kỳ;
            # chỉ tin khi HUD đọc được (auto_on khác None)
            crop = Navigator._crop(frame, self._nav.coords.hud_coords_rect)
            moving: bool | None = None
            if prev_crop is not None and reading.auto_on is not None:
                moving = Navigator._frame_diff(prev_crop, crop) > cfg.arrive_diff_threshold
            prev_crop = crop
            reading = replace(reading, moving=moving)
            action = policy.evaluate(reading)
            log.debug(
                "monitor cycle",
                hp=reading.hp_percent,
                auto=reading.auto_on,
                in_map=reading.in_farm_map,
                action=action.value,
            )
            if action is MonitorAction.SIGNAL_HP_LOW:
                self.signal = FarmSignal.HP_LOW
                log.warning("HP low, signalling retreat", hp=reading.hp_percent)
                return S_DISABLE_AUTO
            if action is MonitorAction.WRONG_MAP:
                log.warning("left the farm map, re-routing")
                return S_OPEN_MAP
            if action is MonitorAction.LOST:
                raise TaskError("monitor lost track of the screen")
            if action is MonitorAction.REENABLE_AUTO:
                log.warning("auto dropped, re-enabling")
                self._nav.set_auto(True)
            if action is MonitorAction.WAKE_AUTO:
                log.warning("auto on but character idle, re-toggling to wake it")
                self._nav.set_auto(False)
                self._nav.set_auto(True)
            # auto nhấn chiêu: chỉ khi chu kỳ này yên ổn — đúng map farm,
            # auto ON, khung hình đọc được (không tap mù lên popup/màn lạ)
            if (
                action is MonitorAction.KEEP_FARMING
                and reading.auto_on is True
                and reading.in_farm_map is not False
            ):
                self._cast_skills(caster, reading)
            time.sleep(cfg.monitor_interval)

    def _unmeasured_skill_slots(self) -> list[int]:
        """Configured skill/heal slots whose screen point is missing or (0, 0)."""
        cfg = self.ctx.settings.farm.skills
        if not cfg.enabled:
            return []
        wanted = list(cfg.slots) + ([cfg.heal_slot] if cfg.heal_slot else [])
        return [
            s
            for s in wanted
            if s > len(self._skill_points) or tuple(self._skill_points[s - 1]) == (0, 0)
        ]

    def _cast_skills(self, caster: SkillCaster, reading: MonitorReading) -> None:
        """Tap due skill/heal slots for this MONITOR cycle.

        Chỉ gọi khi đang farm yên ổn (đúng map, auto ON) — không tap khi màn
        hình lạ/popup (quy tắc #1). Tối đa 2 cú một chu kỳ: thuốc + 1 chiêu.
        Mỗi cú tap đi qua input gate chung (kill-switch, rate-limit, delay).
        """
        cfg = self.ctx.settings.farm.skills
        for _ in range(2):
            slot = caster.next_slot(time.monotonic(), reading.hp_percent)
            if slot is None:
                return
            x, y = self._skill_points[slot - 1]
            if slot == cfg.heal_slot:
                log.info("using heal item", slot=slot, hp=reading.hp_percent)
            else:
                log.debug("casting skill", slot=slot)
            self.ctx.device.tap(x, y)

    def _disable_auto(self) -> str:
        """Best effort: stop fighting before handing over to the runner."""
        try:
            self._nav.set_auto(False)
        except (StateTimeout, VerifyFailed):
            log.warning("auto did not switch off, leaving it to the next task")
        return DONE

    # -- readings ---------------------------------------------------------------

    def _hp(self, frame: Image) -> float | None:
        """HP percent from a frame; ``None`` when unreadable."""
        try:
            return bar_fill_percent(frame, self._hp_rect, self.ctx.settings.farm.hp_bar_color)
        except Exception:  # khung hỏng/đen chỉ là "không đọc được"
            return None

    def _read(self, frame: Image) -> MonitorReading:
        """Collect one cycle's observations from a single frame."""
        hp = self._hp(frame)
        auto_on, _ = self._nav.auto_state(frame)
        in_map: bool | None = self._nav.in_map(self._farm.map, frame=frame)
        # không thấy tên map VÀ cũng không thấy nút auto -> màn hình lạ,
        # đừng kết luận "sai map" trên một khung không đọc được
        if in_map is False and auto_on is None:
            in_map = None
        # số HP chỉ đáng tin khi khung hình thật sự là HUD (có mỏ neo nhìn
        # thấy): hiệu ứng lên cấp / màn load che thanh máu đọc ra 0% GIẢ —
        # từng gây một lần rút về oan (2026-10-26)
        if hp is not None and auto_on is None and in_map is not True:
            hp = None
        return MonitorReading(hp_percent=hp, auto_on=auto_on, in_farm_map=in_map)
