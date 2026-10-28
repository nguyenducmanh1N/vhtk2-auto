"""Map navigation shared by farm (R3) and retreat: open the in-game map,
pick the target map in the "Tìm map" list, tap the destination point ON the
map image, and detect arrival.

The movement itself is the game's OWN auto-pathfinding (core design rule:
the tool orchestrates built-in automation, never re-implements it). Flow
verified LIVE on the AVD (2026-10-26):

    HUD minimap (top-right) -> map screen ("Tìm map"/"Tìm NPC" tabs) ->
    tap a row of the "Tìm map" list to PREVIEW the target map -> tap a
    point on the map image -> the map closes itself, a red travel line
    "Đi: <map>(x, y)" shows at the bottom, and the game auto-paths there
    (cross-map included).

The X/Y input + "Xác nhận" at the bottom was tried first and rejected: it
fails silently on out-of-range coordinates, keeps stale values, and the
soft keyboard shifts the whole layout mid-flow.

Hard-won gotcha: a character SITTING in meditation (đả toạ) cannot
pathfind — the game toasts "Trạng thái hiện tại không thể bắt đầu tự tìm
đường". ``ensure_standing`` must run before any navigation.

Arrival detection needs no OCR (PaddleOCR is absent on the dev Mac):
arrived = the red travel indicator is gone AND the HUD map-name template
matches AND the HUD coordinate line's pixels stay still for N cycles.
"""

from __future__ import annotations

import time
import unicodedata

import cv2
import numpy as np
import structlog

from vhtk.config import FarmCoordinates, FarmSettings
from vhtk.core.errors import StateTimeout, TravelStalled, VerifyFailed
from vhtk.core.fsm import TaskContext
from vhtk.core.vision import Image, Match

log = structlog.get_logger(__name__)

# Tên template của màn bản đồ (UI contract với bộ template đang dùng)
TPL_BTN_CONFIRM_COORDS = "map/btn_confirm_coords"  # nút "Xác nhận" cạnh ô X/Y — cũng là
# chỉ dấu "màn bản đồ đang mở" (chỉ xuất hiện ở màn này)
TPL_BTN_CLOSE_MAP = "map/btn_close"  # nút X đỏ góc phải màn bản đồ
TPL_TAB_FIND_MAP_ON = "map/tab_find_map_on"  # tab "Tìm map" đang active
TPL_TAB_FIND_MAP_OFF = "map/tab_find_map_off"  # tab "Tìm map" chưa active (cần tap)
TPL_TRAVEL_INDICATOR = "map/travel_indicator"  # dòng đỏ "Đi: ..." đáy màn khi đang tìm đường
TPL_BTN_STAND = "map/btn_stand"  # nhãn "Đứng" góc trái dưới = nhân vật ĐANG NGỒI
TPL_BTN_SIT = "map/btn_sit"  # nhãn "Ngồi" = nhân vật đang đứng (bình thường)
TPL_AUTO_ON = "map/btn_auto_on"  # nút auto-đánh trên HUD, trạng thái bật
TPL_AUTO_OFF = "map/btn_auto_off"  # ... trạng thái tắt
TPL_TAB_FIND_NPC_ON = "shop/tab_find_npc_on"  # tab "Tìm NPC" đang active
TPL_TAB_FIND_NPC_OFF = "shop/tab_find_npc_off"  # ... chưa active (cần tap)


def map_slug(name: str) -> str:
    """Slug of an in-game map name, used to derive template names.

    "Long Tuyền Thôn" -> "long_tuyen_thon"; "Thanh Âm động_1" -> "thanh_am_dong_1".
    """
    # NFD tách dấu khỏi chữ; bỏ các ký tự dấu (category Mn); đ/Đ tách riêng
    decomposed = unicodedata.normalize("NFD", name.lower().replace("đ", "d"))
    ascii_name = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    parts = [p for p in "".join(c if c.isalnum() else " " for c in ascii_name).split() if p]
    return "_".join(parts)


def row_template(map_name: str) -> str:
    """Template name of a map's row in the "Tìm map" list."""
    return f"map/row_{map_slug(map_name)}"


def hud_name_template(map_name: str) -> str:
    """Template name of a map's name shown on the HUD (top-right)."""
    return f"map/hud_name_{map_slug(map_name)}"


class Navigator:
    """Drive the in-game map screen; stateless between calls.

    Every method follows rule #1 (find -> tap -> verify) except the taps that
    have no possible template (minimap, X/Y input fields), which use fallback
    coordinates and warn on every use.
    """

    def __init__(self, ctx: TaskContext, coords: FarmCoordinates, cfg: FarmSettings) -> None:
        self.ctx = ctx
        self.coords = coords
        self.cfg = cfg

    # -- character state ----------------------------------------------------------

    def ensure_standing(self) -> None:
        """Stand up if the character is sitting (đả toạ blocks pathfinding).

        The bottom-left HUD button is a toggle: its label reads "Đứng" while
        sitting and "Ngồi" while standing. Missing templates are tolerated
        (old template sets) — the travel then fails loudly at wait_arrival.
        """
        if self.missing_templates(TPL_BTN_STAND, TPL_BTN_SIT):
            log.warning("stand/sit templates missing, skipping stand check")
            return
        # màn bản đồ mở sẵn (sót từ trước / người dùng mở tay) che nút
        # Đứng/Ngồi — recovery cố ý không đóng nó (workspace marker), nên
        # phải tự dọn ở đây trước khi thao tác với HUD
        if self.map_is_open():
            log.info("stray map screen open, closing before stand check")
            self.close_map()
        m = self.ctx.vision.find(TPL_BTN_STAND)
        if m is None:
            return
        log.info("character is sitting, standing up")
        self.ctx.device.tap(*m.center)
        if self.ctx.vision.wait_for(TPL_BTN_SIT, timeout=self.cfg.map_timeout) is None:
            raise VerifyFailed("character did not stand up")

    def auto_state(self, frame: Image | None = None) -> tuple[bool | None, Match | None]:
        """State of the HUD auto-combat button: ``(is_on, button_match)``.

        The on/off templates are near-identical (the button only changes
        color/glow), so a plain threshold match CANNOT tell them apart —
        measured on the AVD both score > 0.9 against either state. Instead,
        both templates are matched on the SAME frame and the higher score
        wins; both below their thresholds means the button is not visible
        (``(None, None)``).
        """
        self._require_templates(TPL_AUTO_ON, TPL_AUTO_OFF)
        shot = frame if frame is not None else self.ctx.vision.capture_screen()
        on = self.ctx.vision.locate(TPL_AUTO_ON, shot)
        off = self.ctx.vision.locate(TPL_AUTO_OFF, shot)
        if (
            on.score < self.ctx.vision.threshold_for(TPL_AUTO_ON)
            and off.score < self.ctx.vision.threshold_for(TPL_AUTO_OFF)
        ):
            return None, None
        # hai điểm quá sát nhau = khung hình mập mờ (hiệu ứng, animation) —
        # KHÔNG kết luận, vì đoán sai sẽ dẫn tới tap làm lật trạng thái thật
        if abs(on.score - off.score) < self.cfg.auto_state_margin:
            return None, None
        return (True, on) if on.score >= off.score else (False, off)

    def set_auto(self, want_on: bool) -> None:
        """Switch the game's auto-combat to the wanted state (find-tap-verify).

        Raises:
            StateTimeout: the auto button is not visible / state unreadable.
            VerifyFailed: the button did not reach the wanted state in time.
        """
        state, btn = self._read_auto_stable()
        if state is None or btn is None:
            raise StateTimeout("auto button not found or state unreadable")
        if state == want_on:
            return
        self.ctx.device.tap(*btn.center)
        deadline = time.monotonic() + self.cfg.map_timeout
        while time.monotonic() < deadline:
            if self.auto_state()[0] == want_on:
                log.info("auto-combat switched", on=want_on)
                return
            time.sleep(self.cfg.arrive_check_interval)
        raise VerifyFailed(f"auto did not switch {'on' if want_on else 'off'}")

    def _read_auto_stable(self, attempts: int = 3) -> tuple[bool | None, Match | None]:
        """Like :meth:`auto_state` but retries a few frames: one ambiguous
        frame (effect/animation over the button) must not fail a whole state."""
        for _ in range(attempts):
            state, btn = self.auto_state()
            if state is not None:
                return state, btn
            time.sleep(self.cfg.arrive_check_interval)
        return None, None

    def wait_movement(self, timeout: float) -> bool:
        """True if the HUD coordinate line changed within ``timeout``, i.e.
        the character moved (auto-combat went hunting); False if it stayed
        put the whole time."""
        deadline = time.monotonic() + timeout
        prev: Image | None = None
        while time.monotonic() < deadline:
            self.ctx.safety.check_kill_switch()
            frame = self.ctx.vision.capture_screen()
            crop = self._crop(frame, self.coords.hud_coords_rect)
            if prev is not None and self._frame_diff(prev, crop) > self.cfg.arrive_diff_threshold:
                return True
            prev = crop
            time.sleep(self.cfg.arrive_check_interval)
        return False

    def ensure_sitting(self) -> None:
        """Sit down (đả toạ) — HP regenerates far faster than standing.

        Mirror of :meth:`ensure_standing`; used while resting in town.
        Missing templates are tolerated (resting standing still works,
        just slower).

        Raises:
            VerifyFailed: the character did not sit down after the tap.
        """
        if self.missing_templates(TPL_BTN_STAND, TPL_BTN_SIT):
            log.warning("stand/sit templates missing, resting standing up")
            return
        if self.map_is_open():
            log.info("stray map screen open, closing before sit check")
            self.close_map()
        m = self.ctx.vision.find(TPL_BTN_SIT)
        if m is None:
            return  # đã ngồi sẵn (hoặc nút bị che — nghỉ đứng cũng không sao)
        log.info("sitting down to rest")
        self.ctx.device.tap(*m.center)
        if self.ctx.vision.wait_for(TPL_BTN_STAND, timeout=self.cfg.map_timeout) is None:
            raise VerifyFailed("character did not sit down")

    # -- map screen lifecycle ----------------------------------------------------

    def map_is_open(self, frame: Image | None = None) -> bool:
        """The map screen is open iff its coordinate-confirm button shows."""
        self._require_templates(TPL_BTN_CONFIRM_COORDS)
        return self.ctx.vision.find(TPL_BTN_CONFIRM_COORDS, screenshot=frame) is not None

    def open_map(self) -> None:
        """Open the map screen from the HUD (idempotent).

        Raises:
            StateTimeout: the map screen did not appear.
        """
        if self.map_is_open():
            return
        log.warning("using fallback coordinates", target="minimap", xy=self.coords.minimap)
        self.ctx.device.tap(*self.coords.minimap)
        if self.ctx.vision.wait_for(TPL_BTN_CONFIRM_COORDS, timeout=self.cfg.map_timeout) is None:
            raise StateTimeout("map screen did not open after tapping minimap")

    def close_map(self) -> None:
        """Close the map screen if open; back key as fallback.

        Raises:
            VerifyFailed: the map screen refused to close.
        """
        if not self.map_is_open():
            return
        btn = None
        if TPL_BTN_CLOSE_MAP in self.ctx.vision.store:
            btn = self.ctx.vision.find(TPL_BTN_CLOSE_MAP)
        if btn is not None:
            self.ctx.device.tap(*btn.center)
        else:
            log.warning("map close button template missing, using back key")
            self.ctx.device.press_back()
        if not self.ctx.vision.wait_vanish(TPL_BTN_CONFIRM_COORDS, timeout=self.cfg.map_timeout):
            raise VerifyFailed("map screen did not close")

    def ensure_find_map_tab(self) -> None:
        """Switch the map screen's left panel to the "Tìm map" tab (idempotent).

        Dùng cho các tool vận hành (harvest/calibrate) tap dòng theo vị trí —
        luồng task thường dùng ``_select_row`` (tự chuyển tab theo template dòng).

        Raises:
            StateTimeout: tabs not visible (map screen not open?).
        """
        hit = self.ctx.vision.wait_any(
            [TPL_TAB_FIND_MAP_ON, TPL_TAB_FIND_MAP_OFF], timeout=self.cfg.map_timeout
        )
        if hit is None:
            raise StateTimeout("map tabs not visible — is the map screen open?")
        if hit[0] == TPL_TAB_FIND_MAP_OFF:
            self.ctx.device.tap(*hit[1].center)
            time.sleep(1.0)

    # -- target selection ----------------------------------------------------------

    def select_map(self, map_name: str) -> None:
        """In the open map screen, pick ``map_name`` in the "Tìm map" list.

        Raises:
            StateTimeout: tab or row not found/clickable in time.
        """
        # v1: dòng map phải nhìn thấy được không cần cuộn — map farm nên chọn
        # trong danh sách map lân cận của thành
        self._select_row(
            row_template(map_name), TPL_TAB_FIND_MAP_ON, TPL_TAB_FIND_MAP_OFF, '"Tìm map"'
        )

    def select_npc(self, row_template: str) -> None:
        """In the open map screen, tap an NPC row in the "Tìm NPC" list.

        The game then auto-routes the character to that NPC and shows its
        target plate (with a talk icon) when close; the map closes itself.

        Raises:
            StateTimeout: tab or NPC row not visible in time.
        """
        self._select_row(
            row_template, TPL_TAB_FIND_NPC_ON, TPL_TAB_FIND_NPC_OFF, '"Tìm NPC"'
        )

    def _select_row(self, row_tpl: str, tab_on: str, tab_off: str, label: str) -> None:
        """Tap a row of the map screen's left list, switching tabs if needed.

        Bằng chứng tab đã active là CHÍNH DÒNG cần tìm hiện ra, không phải
        template tab_on: dòng chạy thông báo (ticker) của game trườn ngang
        qua hàng tab và che nó bất kỳ lúc nào (lỗi thực tế 26/10 — sell kẹt
        lặp vô tận ở '"Tìm NPC" tab did not activate' dù list NPC đã mở).
        """
        self._require_templates(row_tpl, tab_on, tab_off)
        # PHẢI poll chứ không chụp một khung: màn bản đồ vừa mở còn đang
        # animation, panel trái chưa render xong (lỗi thực tế 2026-10-26)
        row = self.ctx.vision.find(row_tpl)
        if row is None:
            hit = self.ctx.vision.wait_any([tab_on, tab_off], timeout=self.cfg.map_timeout)
            if hit is None:
                raise StateTimeout(f"{label} tab not found on map screen")
            if hit[0] == tab_off:
                self.ctx.device.tap(*hit[1].center)
            row = self.ctx.vision.wait_for(row_tpl, timeout=self.cfg.map_timeout)
        if row is None:
            raise StateTimeout(
                f"row {row_tpl!r} not visible in {label} list (scrolling not implemented)"
            )
        self.ctx.device.tap(*row.center)

    def resolve_spot(self, spot: tuple[int, int]) -> tuple[int, int]:
        """Screen pixel to tap on the map image for a configured spot.

        ``(0, 0)`` = chưa cấu hình -> tâm vùng ảnh bản đồ (trung tâm map
        đích); giá trị khác giữ nguyên.
        """
        if spot != (0, 0):
            return spot
        x, y, w, h = self.coords.map_image_rect
        center = (x + w // 2, y + h // 2)
        log.info("spot not configured, using map image center", center=center)
        return center

    def tap_map_point(self, x: int, y: int) -> None:
        """Tap the destination point ON the map image to start pathfinding.

        ``(x, y)`` is a SCREEN pixel inside the map image area (the account
        config stores it; the operator picks it visually from a map
        screenshot). On success the game closes the map screen itself and
        starts auto-pathing — verified by the map vanishing.

        Raises:
            VerifyFailed: the map screen stayed open (tap did not register
                or the point is not walkable).
        """
        self._require_templates(TPL_BTN_CONFIRM_COORDS)
        # map đã bị đóng (vd recovery tưởng là popup) thì tap (x, y) sẽ rơi
        # thẳng vào thế giới — tap mù, cấm tuyệt đối (quy tắc #1)
        if not self.map_is_open():
            raise VerifyFailed("map screen is not open, refusing to tap a world point")
        # game hay nuốt tap ĐẦU TIÊN vào ảnh bản đồ ngay sau khi preview vừa
        # đổi map (quan sát AVD 26/10: tap 1 trượt, tap 2 ăn) — nên tap lại
        # tại chỗ thay vì fail cả state rồi đi vòng qua recovery
        attempts = self.cfg.map_spot_tap_attempts
        for attempt in range(1, attempts + 1):
            log.warning(
                "using fallback coordinates", target="map_spot", xy=(x, y), attempt=attempt
            )
            self.ctx.device.tap(x, y)
            # map tự đóng = tìm đường đã bắt đầu; còn mở = tap không ăn
            if self.ctx.vision.wait_vanish(TPL_BTN_CONFIRM_COORDS, timeout=self.cfg.map_timeout):
                return
        raise VerifyFailed(
            f"map stayed open after tapping the destination point ({attempts} attempts)"
        )

    def unstick_via_npc(self, npc_row: str, walk_seconds: float) -> bool:
        """Route to a known NPC to pull the character off a dead spot.

        Đứng NGAY CỔNG map, lệnh đi xuyên map bị game từ chối âm thầm nhưng
        lệnh đi tới NPC vẫn ăn (đo thực tế 26/10, kẹt ở 197/175 cổng bắc
        Tuyền Châu). Đi vài giây cho rời cổng là đủ — không cần tới nơi;
        lệnh điều hướng kế tiếp sẽ đè lên tuyến đang đi.

        Returns:
            True nếu nhân vật đã bắt đầu đi; False nếu không gỡ được
            (NPC row không có trong map hiện tại, hoặc vẫn đứng yên).
        """
        try:
            self.open_map()
            self.select_npc(npc_row)
        except (StateTimeout, VerifyFailed) as exc:
            log.warning("unstick via NPC failed", reason=str(exc))
            return False
        if not self.travel_started(self.cfg.travel_start_timeout):
            log.warning("unstick via NPC failed", reason="character still not moving")
            return False
        log.info("unstick: walking toward NPC to leave the dead spot")
        deadline = time.monotonic() + walk_seconds
        while time.monotonic() < deadline:
            self.ctx.safety.check_kill_switch()
            time.sleep(self.cfg.arrive_check_interval)
        return True

    def travel_started(self, timeout: float) -> bool:
        """True once the game shows signs of ACTIVE pathfinding.

        Map closing is NOT enough: right after login the game sometimes
        closes the map without starting to pathfind (observed on the AVD
        26/10). Started = the red "Đi: ..." indicator shows (when its
        template exists) OR the HUD coordinate line changed (character
        moving).
        """
        check_indicator = TPL_TRAVEL_INDICATOR in self.ctx.vision.store
        deadline = time.monotonic() + timeout
        prev: Image | None = None
        while time.monotonic() < deadline:
            self.ctx.safety.check_kill_switch()
            frame = self.ctx.vision.capture_screen()
            if check_indicator and (
                self.ctx.vision.find(TPL_TRAVEL_INDICATOR, screenshot=frame) is not None
            ):
                return True
            crop = self._crop(frame, self.coords.hud_coords_rect)
            if prev is not None and self._frame_diff(prev, crop) > self.cfg.arrive_diff_threshold:
                return True
            prev = crop
            time.sleep(self.cfg.arrive_check_interval)
        return False

    # -- position checks ---------------------------------------------------------

    def in_map(self, map_name: str, frame: Image | None = None) -> bool:
        """True if the HUD map name matches ``map_name`` (template, no OCR)."""
        tpl = hud_name_template(map_name)
        self._require_templates(tpl)
        return self.ctx.vision.find(tpl, screenshot=frame) is not None

    def wait_arrival(self, map_name: str) -> None:
        """Wait until the character stands still inside ``map_name``.

        Arrived = the red "Đi: ..." travel indicator is gone (when its
        template exists) AND the HUD map name matches AND the HUD coordinate
        line's pixels stayed unchanged for ``arrive_stable_cycles``
        consecutive checks.

        Raises:
            TravelStalled: the character stood still ``travel_stall_cycles``
                checks in a row with no travel indicator while still OUTSIDE
                ``map_name`` — game dừng tìm đường ở cổng map (lỗi thực tế
                26/10, kẹt ở 197/175 cổng bắc Tuyền Châu); caller nên điều
                hướng lại thay vì chờ hết giờ.
            StateTimeout: not arrived within ``travel_timeout``.
        """
        check_indicator = TPL_TRAVEL_INDICATOR in self.ctx.vision.store
        deadline = time.monotonic() + self.cfg.travel_timeout
        stable = 0
        prev: Image | None = None
        while time.monotonic() < deadline:
            self.ctx.safety.check_kill_switch()
            frame = self.ctx.vision.capture_screen()
            crop = self._crop(frame, self.coords.hud_coords_rect)
            if prev is not None and self._frame_diff(prev, crop) <= self.cfg.arrive_diff_threshold:
                stable += 1
            else:
                stable = 0
            prev = crop
            still_travelling = check_indicator and (
                self.ctx.vision.find(TPL_TRAVEL_INDICATOR, screenshot=frame) is not None
            )
            if stable >= self.cfg.arrive_stable_cycles and not still_travelling:
                if self.in_map(map_name, frame=frame):
                    log.info("arrived", map=map_name)
                    return
                if stable >= self.cfg.travel_stall_cycles:
                    raise TravelStalled(
                        f"character stopped outside {map_name!r} with no travel indicator"
                    )
            time.sleep(self.cfg.arrive_check_interval)
        raise StateTimeout(f"did not arrive in {map_name!r} within {self.cfg.travel_timeout}s")

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _crop(frame: Image, rect: tuple[int, int, int, int]) -> Image:
        x, y, w, h = rect
        return frame[y : y + h, x : x + w]

    @staticmethod
    def _frame_diff(a: Image, b: Image) -> float:
        """Fraction (0..1) of clearly-changed pixels between two equal crops."""
        if a.shape != b.shape:
            return 1.0
        ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY) if a.ndim == 3 else a
        gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY) if b.ndim == 3 else b
        changed = np.count_nonzero(cv2.absdiff(ga, gb) > 30)
        return changed / ga.size

    def missing_templates(self, *names: str) -> list[str]:
        """Which of ``names`` are absent from the active template store."""
        return [n for n in names if n not in self.ctx.vision.store]

    def _require_templates(self, *names: str) -> None:
        missing = self.missing_templates(*names)
        if missing:
            raise VerifyFailed(
                f"missing templates {missing} — capture them with tools/capture.py "
                "(see tasks/navigate.py docstring for the naming contract)"
            )
