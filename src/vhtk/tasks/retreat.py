"""Retreat when HP is low: walk back to the configured safe spot (town) via
the map screen and rest until HP regenerates (companion of FarmTask, R3).

Chosen design (2026-10-25): retreat by the game's own map pathfinding — no
town scroll consumed, no bag templates needed. The character may die on the
way when HP is very low; recovery's revive handling plus the task's RECOVER
quota covers that (revive -> resume at VERIFY_HUD -> route again).

The runner drives the cycle: FarmTask signals HP_LOW -> RetreatTask -> back
to FarmTask. This task never decides anything beyond "am I rested yet".

Speed matters here — the character stands among mobs with low HP until
pathfinding starts. Each screenshot costs ~1s on the AVD, so VERIFY_HUD reads
everything it can from ONE frame (screen, auto, sitting, current map) and
NAVIGATE does auto-off + open map + pick map + tap spot in one state (one
recovery pass instead of four). Auto-off is tapped without waiting and
verified once travel has started (measured 2026-11-07: ~23 -> ~11 shots).
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import structlog

from vhtk.config import Coordinates, load_coordinates
from vhtk.core.errors import (
    StateTimeout,
    TaskDefinitionError,
    TaskError,
    TravelStalled,
    VerifyFailed,
)
from vhtk.core.fsm import ABORT, DONE, BaseTask, State, TaskContext
from vhtk.core.vision import Image, bar_fill_percent
from vhtk.mapstore import MapStore, maps_dir
from vhtk.tasks.farm import TPL_HUD
from vhtk.tasks.navigate import (
    TPL_AUTO_OFF,
    TPL_AUTO_ON,
    TPL_BTN_CONFIRM_COORDS,
    TPL_BTN_SIT,
    TPL_BTN_STAND,
    TPL_TAB_FIND_MAP_OFF,
    TPL_TAB_FIND_MAP_ON,
    Navigator,
)

log = structlog.get_logger(__name__)

S_VERIFY_HUD = "VERIFY_HUD"
S_NAVIGATE = "NAVIGATE"
S_TRAVEL = "TRAVEL_WAIT"
S_REST = "REST"


class RetreatTask(BaseTask):
    """Walk to the account's retreat spot and rest until HP recovers."""

    name = "retreat"

    def __init__(self, ctx: TaskContext) -> None:
        if ctx.account is None:
            raise TaskDefinitionError("RetreatTask requires ctx.account")
        self._farm = ctx.account.farm
        coords: Coordinates = load_coordinates(ctx.settings.paths.coordinates_file)
        store = MapStore(maps_dir(ctx.settings.templates.version))
        self._nav = Navigator(ctx, coords.farm, ctx.settings.farm, store=store)
        self._hp_rect = coords.farm.hp_bar_rect
        self._nav_attempts = 0
        # khung VERIFY_HUD vừa chụp — NAVIGATE dùng lại 1 lần cho đỡ chụp
        self._hud_frame: Image | None = None
        super().__init__(ctx)

    def states(self) -> Sequence[State]:
        cfg = self.ctx.settings.farm
        return [
            State(S_VERIFY_HUD, self._verify_hud, 30.0, frozenset({S_NAVIGATE})),
            State(
                S_NAVIGATE,
                self._navigate,
                3 * cfg.map_timeout + cfg.travel_start_timeout,
                frozenset({S_TRAVEL, S_NAVIGATE, S_REST}),
            ),
            State(
                S_TRAVEL,
                self._travel_wait,
                cfg.travel_timeout,
                frozenset({S_REST, S_NAVIGATE}),
            ),
            State(S_REST, self._rest, cfg.rest_timeout, frozenset({DONE})),
        ]

    def initial_state(self) -> str:
        return S_VERIFY_HUD

    def recovery_resume_state(self, failed_state: str) -> str:
        """After recovery (revive included) the position is unknown — re-route."""
        return S_VERIFY_HUD

    # -- state handlers ----------------------------------------------------------

    def _verify_hud(self) -> str:
        if self._farm.retreat_spot == (0, 0) or not self._farm.retreat_map:
            log.error(
                "retreat spot/map not configured for this account",
                account=self.ctx.account.name if self.ctx.account else "?",
            )
            return ABORT
        missing = self._nav.missing_templates(
            TPL_HUD,
            TPL_BTN_CONFIRM_COORDS,
            TPL_TAB_FIND_MAP_ON,
            TPL_TAB_FIND_MAP_OFF,
            TPL_AUTO_ON,
            TPL_AUTO_OFF,
        )
        # map rút về: cần cặp template row/hud_name HOẶC mục trong kho data/maps
        missing += self._nav.map_requirements_missing(self._farm.retreat_map)
        if missing:
            log.error(
                "retreat prerequisites missing — capture templates or harvest maps",
                missing=missing,
            )
            return ABORT
        # nhiều mỏ neo HUD — như FarmTask: chỗ đông người che mất nút Auto
        anchors = [TPL_HUD, TPL_BTN_SIT, TPL_BTN_STAND, TPL_BTN_CONFIRM_COORDS]
        # thử 1 khung trước và GIỮ nó cho NAVIGATE (đọc auto/ngồi/map không
        # tốn thêm lần chụp); chưa thấy mỏ neo nào mới poll như cũ
        frame: Image | None = self.ctx.vision.capture_screen()
        hit_name = next(
            (n for n in anchors if self.ctx.vision.find(n, screenshot=frame) is not None), None
        )
        if hit_name is None:
            hit = self.ctx.vision.wait_any(anchors, timeout=20.0)
            if hit is None:
                raise StateTimeout("neither HUD nor map screen is showing")
            hit_name, frame = hit[0], None
        if hit_name == TPL_BTN_CONFIRM_COORDS:
            self._nav.close_map()
            frame = None
        self._hud_frame = frame
        return S_NAVIGATE

    def _navigate(self) -> str:
        """Auto off -> stand -> open map -> pick the town -> tap the spot.

        One state on purpose: every state boundary costs a recovery
        screenshot, and the character is taking hits until it walks off.
        """
        cfg = self.ctx.settings.farm
        frame, self._hud_frame = self._hud_frame, None
        # tắt auto: auto còn bật thì game lôi nhân vật quay lại đánh quái.
        # Tap KHÔNG chờ — xác minh dời tới sau khi đã chạy (_confirm_auto_off)
        auto_on, btn = self._nav.auto_state(frame)
        if auto_on and btn is not None:
            # tap auto chỉ đổi NÚT auto — nút Ngồi/Đứng, tên map, nút bản đồ
            # trong khung vẫn đúng, nên giữ khung cho các kiểm tra dưới
            self.ctx.device.tap(*btn.center)
        # nhân vật ngồi (đả toạ) thì game từ chối tìm đường — đứng dậy trước
        self._nav.ensure_standing(frame)
        here = self._nav.in_map(self._farm.retreat_map, frame=frame)
        self._nav.open_map(frame)
        if not here:
            self._nav.select_map(self._farm.retreat_map)
        x, y = self._nav.resolve_spot(self._farm.retreat_spot)
        try:
            self._nav.tap_map_point(x, y)
        except VerifyFailed:
            # tap điểm đến trùng/sát chỗ đang đứng thì game LỜ ĐI và map cứ
            # mở (lỗi thực tế 26/10: login xong spawn ngay điểm nghỉ) — đã
            # ở trong map thành thì nghỉ tại chỗ là đủ an toàn. Map từ kho
            # (in_map=None): confirm tự đóng map rồi mở MỚI so tiêu đề
            here = self._nav.in_map(self._farm.retreat_map)
            if here is None:
                here = self._nav.confirm_in_map(self._farm.retreat_map)
            if here:
                log.info("already in retreat map, resting in place")
                self._nav.close_map()
                return S_REST
            raise
        # như FarmTask: map đóng chưa chắc đã đi — xác minh tìm đường bắt đầu
        if not self._nav.travel_started(cfg.travel_start_timeout):
            self._nav_attempts += 1
            if self._nav_attempts > cfg.nav_retries:
                # ABORT chứ KHÔNG raise — như FarmTask: raise qua RECOVER sẽ
                # lặp vô hạn vì quota recover reset theo transition thành công
                log.error("pathfinding never started, giving up", attempts=self._nav_attempts)
                return ABORT
            # như FarmTask: nghi kẹt cổng map từ lần thử 2 — gỡ bằng NPC
            if self._nav_attempts >= 2 and cfg.unstick_npc_row:
                self._nav.unstick_via_npc(cfg.unstick_npc_row, cfg.unstick_walk_seconds)
            log.warning("travel did not start, redoing navigation", attempt=self._nav_attempts)
            return S_NAVIGATE
        # KHÔNG reset bộ đếm ở đây: travel_started có thể "thành công" giả vì
        # dòng "Đi:" cũ trong chat — chỉ reset khi tới nơi / đã tiến được
        self._confirm_auto_off()
        return S_TRAVEL

    def _confirm_auto_off(self) -> None:
        """Verify step of the deferred auto-off tap, now that we are moving."""
        try:
            self._nav.set_auto(False)
        except (StateTimeout, VerifyFailed):
            log.warning("auto did not switch off during retreat")

    def _travel_wait(self) -> str:
        try:
            self._nav.wait_arrival(self._farm.retreat_map)
        except TravelStalled as exc:
            # game dừng ở cổng map — tap lại điểm đến từ chỗ đang đứng
            # đã tiến được một đoạn (vượt cổng) thì tính là lần kẹt MỚI; đứng
            # im tại chỗ thì cộng dồn tới nav_retries rồi bỏ cuộc
            self._nav_attempts = 1 if exc.moved else self._nav_attempts + 1
            if self._nav_attempts > self.ctx.settings.farm.nav_retries:
                log.error("travel keeps stalling, giving up", attempts=self._nav_attempts)
                return ABORT
            log.warning("travel stalled, redoing navigation", attempt=self._nav_attempts)
            return S_NAVIGATE
        self._nav_attempts = 0
        return S_REST

    def _rest(self) -> str:
        """Sit still until HP regenerates past ``hp_resume_percent``.

        Also watches the LOCATION every cycle: the game's story-quest
        auto-routing can grab an idle character and haul it to another city
        (observed live: repeatedly dragged to Tương Dương while resting).
        Two consecutive cycles outside the retreat map -> back to RECOVER,
        which re-routes home instead of resting in the wrong city.

        Raises:
            TaskError: pulled out of the retreat map while resting.
            StateTimeout: HP did not recover within ``rest_timeout`` — the
                recovery pass then looks for a revive button (we may be dead).
        """
        cfg = self.ctx.settings.farm
        # ngồi đả toạ hồi máu nhanh gấp nhiều lần đứng (đo thực tế: đứng hồi
        # 26% -> 84% mất hơn 10 phút, quá rest_timeout); ngồi hỏng thì nghỉ đứng
        try:
            self._nav.ensure_sitting()
        except VerifyFailed:
            log.warning("could not sit down, resting standing up")
        deadline = time.monotonic() + cfg.rest_timeout
        away_streak = 0
        while time.monotonic() < deadline:
            self.ctx.safety.check_kill_switch()
            frame = self.ctx.vision.capture_screen()
            try:
                hp = bar_fill_percent(frame, self._hp_rect, cfg.hp_bar_color)
            except Exception:
                hp = None
            if hp is not None and hp >= self._farm.hp_resume_percent:
                log.info("rested, HP recovered", hp=round(hp, 1))
                return DONE
            # nhân vật hay bị DỰNG DẬY giữa chừng ở thành đông (quest tân thủ
            # auto-route, người chơi khác tác động — quan sát 27/10: ngồi xong
            # vài giây lại đứng, hồi máu rù rì tới hết rest_timeout) — thấy
            # nút "Ngồi" hiện lại nghĩa là đang đứng: ngồi lại ngay
            if self.ctx.vision.find(TPL_BTN_SIT, screenshot=frame) is not None:
                log.warning("stood up while resting, sitting down again")
                try:
                    self._nav.ensure_sitting()
                except VerifyFailed:
                    log.warning("could not sit down, resting standing up")
            # 2 chu kỳ liên tiếp không thấy tên thành (1 khung có thể bị che);
            # map từ kho trả None = không đọc được vị trí theo khung hình —
            # bỏ qua (mở bản đồ so tiêu đề mỗi chu kỳ sẽ phá giấc nghỉ)
            here = self._nav.in_map(self._farm.retreat_map, frame=frame)
            if here:
                away_streak = 0
            elif here is False:
                away_streak += 1
                if away_streak >= 2:
                    raise TaskError("pulled out of the retreat map while resting")
            time.sleep(cfg.monitor_interval)
        raise StateTimeout(
            f"HP did not reach {self._farm.hp_resume_percent}% within {cfg.rest_timeout}s"
        )
