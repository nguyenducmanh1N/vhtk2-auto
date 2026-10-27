"""R3b: sell junk loot to the NPC grocer (PLAN.md 4.3).

Flow verified LIVE on the AVD (2026-10-26):

    map screen -> "Tìm NPC" tab -> row "Chủ Tạp hóa" -> the game auto-routes
    to the NPC and shows its target plate (talk icon) -> tap the talk icon ->
    "Cửa hiệu" window opens WITH the bag side by side -> tap "Bán" -> every
    sellable bag cell gets a yellow-green highlight -> TAP A CELL = SOLD
    INSTANTLY, the game never asks for confirmation.

Because the game confirms nothing, every protection lives in the tool,
stacked (rule #4 of CLAUDE.md):

1. ``dry_run`` (default ON) — scan and log "would sell", never tap.
2. ``confirm_mode`` — a human approves via notifier; no notifier = refusal.
3. **WHITELIST**: a cell is sellable only if its icon matches one of the
   ``sell/ok_*`` templates — unknown/new items are NEVER sold. (Chosen over
   a blacklist on 2026-10-26: safer when the game drops unseen items.)
4. Quality gate: border color >= ``quality_blacklist_threshold`` blocks.
5. ``guard_irreversible`` per item — session cap catches tap loops.

The runner calls this task while the character already rests in town
(piggybacked on the retreat cycle, every ``sell.every_n_retreats`` cycles).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

import cv2
import structlog

from vhtk.config import Coordinates, SellSettings, load_coordinates
from vhtk.core.errors import StateTimeout, TaskDefinitionError
from vhtk.core.fsm import ABORT, DONE, BaseTask, State, TaskContext
from vhtk.core.vision import Image, detect_quality
from vhtk.tasks.farm import TPL_HUD
from vhtk.tasks.navigate import (
    TPL_BTN_CONFIRM_COORDS,
    TPL_BTN_SIT,
    TPL_BTN_STAND,
    TPL_TAB_FIND_NPC_OFF,
    TPL_TAB_FIND_NPC_ON,
    Navigator,
)

log = structlog.get_logger(__name__)

# UI contract của flow bán đồ
TPL_SHOP_HEADER = "shop/cua_hieu_header"  # tiêu đề "Cửa hiệu" = shop đang mở
TPL_BTN_SELL_MODE = "shop/btn_ban"  # nút "Bán" trong shop
TPL_BTN_CLOSE_SHOP = "shop/btn_close_shop"  # nút X của khung Cửa hiệu
TPL_NPC_TALK = "shop/npc_talk_icon"  # icon 💬 trên bảng mục tiêu NPC
TPL_NPC_ROW = "shop/row_chu_tap_hoa"  # dòng "Chủ Tạp hóa" trong tab Tìm NPC
WHITELIST_PREFIX = "sell/ok_"  # icon đồ RÁC được phép bán

S_VERIFY_HUD = "VERIFY_HUD"
S_GO_TO_NPC = "GO_TO_NPC"
S_OPEN_SHOP = "OPEN_SHOP"
S_MODE_SELL = "MODE_SELL"
S_SELL_SCAN = "SELL_SCAN"
S_CLOSE = "CLOSE_SHOP"


@dataclass
class SellReport:
    """What one sell run did — consumed by the runner's log."""

    sold: int = 0
    would_sell: int = 0  # dry_run / confirm bị từ chối
    skipped_unknown: int = 0  # không khớp whitelist -> giữ lại
    skipped_quality: int = 0  # phẩm chất >= ngưỡng -> giữ lại
    details: list[str] = field(default_factory=list)


def cell_highlight_fraction(cell: Image, cfg: SellSettings) -> float:
    """Fraction of a cell's pixels inside the sell-mode highlight color."""
    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].astype(int)
    sat = hsv[:, :, 1].astype(int)
    val = hsv[:, :, 2].astype(int)
    rng = cfg.highlight
    h_lo, h_hi = rng.h
    # hue là vòng tròn 0..179 — dải quấn vòng (h_lo > h_hi) hợp lệ
    h_ok = (hue >= h_lo) & (hue <= h_hi) if h_lo <= h_hi else (hue >= h_lo) | (hue <= h_hi)
    mask = (
        h_ok
        & (sat >= rng.s[0])
        & (sat <= rng.s[1])
        & (val >= rng.v[0])
        & (val <= rng.v[1])
    )
    return float(mask.mean())


def best_whitelist_match(
    cell_gray: Image, whitelist: dict[str, Image], threshold: float
) -> str | None:
    """Name of the best-matching ``sell/ok_*`` icon, or ``None`` (don't sell).

    Pure function — the heart of the safety decision, unit-tested offline.
    """
    best_name: str | None = None
    best_score = threshold
    for name, icon in whitelist.items():
        ih, iw = icon.shape[:2]
        if ih > cell_gray.shape[0] or iw > cell_gray.shape[1]:
            continue
        score = float(cv2.matchTemplate(cell_gray, icon, cv2.TM_CCOEFF_NORMED).max())
        if score >= best_score:
            best_name, best_score = name, score
    return best_name


class SellTask(BaseTask):
    """Sell whitelisted junk to the NPC grocer; report in :attr:`report`."""

    name = "sell"

    def __init__(self, ctx: TaskContext) -> None:
        if ctx.account is None:
            raise TaskDefinitionError("SellTask requires ctx.account")
        coords: Coordinates = load_coordinates(ctx.settings.paths.coordinates_file)
        self._nav = Navigator(ctx, coords.farm, ctx.settings.farm)
        self._sc = coords.sell
        self.report = SellReport()
        super().__init__(ctx)

    def states(self) -> Sequence[State]:
        cfg = self.ctx.settings.sell
        return [
            State(S_VERIFY_HUD, self._verify_hud, 30.0, frozenset({S_GO_TO_NPC})),
            State(S_GO_TO_NPC, self._go_to_npc, cfg.npc_route_timeout, frozenset({S_OPEN_SHOP})),
            State(S_OPEN_SHOP, self._open_shop, cfg.shop_timeout, frozenset({S_MODE_SELL})),
            State(S_MODE_SELL, self._mode_sell, cfg.shop_timeout, frozenset({S_SELL_SCAN})),
            State(S_SELL_SCAN, self._sell_scan, 120.0, frozenset({S_CLOSE})),
            State(S_CLOSE, self._close, cfg.shop_timeout, frozenset({DONE})),
        ]

    def initial_state(self) -> str:
        return S_VERIFY_HUD

    def recovery_resume_state(self, failed_state: str) -> str:
        """Screen may be anywhere after recovery — re-route from the HUD."""
        return S_VERIFY_HUD

    # -- state handlers ----------------------------------------------------------

    def _verify_hud(self) -> str:
        self.report = SellReport()  # chạy lại cùng instance không giữ số liệu cũ
        missing = self._nav.missing_templates(
            TPL_SHOP_HEADER,
            TPL_BTN_SELL_MODE,
            TPL_BTN_CLOSE_SHOP,
            TPL_NPC_TALK,
            TPL_NPC_ROW,
            TPL_BTN_CONFIRM_COORDS,
            TPL_TAB_FIND_NPC_ON,
            TPL_TAB_FIND_NPC_OFF,
        )
        whitelist = self._whitelist_names()
        if missing or not whitelist:
            log.error(
                "sell templates missing — capture them with tools/capture.py",
                missing=missing,
                whitelist_count=len(whitelist),
            )
            return ABORT
        hit = self.ctx.vision.wait_any(
            [TPL_HUD, TPL_BTN_SIT, TPL_BTN_STAND, TPL_SHOP_HEADER, TPL_BTN_CONFIRM_COORDS],
            timeout=20.0,
        )
        if hit is None:
            raise StateTimeout("neither HUD, shop nor map screen is showing")
        if hit[0] == TPL_SHOP_HEADER:
            self._close_shop_windows()
        elif hit[0] == TPL_BTN_CONFIRM_COORDS:
            self._nav.close_map()
        return S_GO_TO_NPC

    def _go_to_npc(self) -> str:
        """Let the game route us to the grocer via the map's NPC list."""
        cfg = self.ctx.settings.sell
        self._nav.ensure_standing()
        self._nav.open_map()
        self._nav.select_npc(TPL_NPC_ROW)
        # map tự đóng, game tự tìm đường; bảng mục tiêu NPC (icon 💬) hiện
        # khi đã đứng cạnh NPC
        if self.ctx.vision.wait_for(TPL_NPC_TALK, timeout=cfg.npc_route_timeout) is None:
            raise StateTimeout("NPC talk icon did not appear after routing")
        return S_OPEN_SHOP

    def _open_shop(self) -> str:
        cfg = self.ctx.settings.sell
        # icon 💬 chập chờn khi thành đông người che bảng mục tiêu — chờ nó
        # hiện lại thay vì fail ngay (lỗi thực tế 26/10, tốn 1 vòng recovery)
        talk = self.ctx.vision.wait_for(TPL_NPC_TALK, timeout=cfg.shop_timeout)
        if talk is None:
            raise StateTimeout("NPC talk icon vanished before opening the shop")
        self.ctx.device.tap(*talk.center)
        if self.ctx.vision.wait_for(TPL_SHOP_HEADER, timeout=cfg.shop_timeout) is None:
            raise StateTimeout("shop window did not open")
        return S_MODE_SELL

    def _mode_sell(self) -> str:
        cfg = self.ctx.settings.sell
        btn = self.ctx.vision.wait_for(TPL_BTN_SELL_MODE, timeout=cfg.shop_timeout)
        if btn is None:
            raise StateTimeout("sell button not found in shop")
        self.ctx.device.tap(*btn.center)
        time.sleep(1.0)  # chờ overlay highlight vẽ xong
        return S_SELL_SCAN

    def _sell_scan(self) -> str:
        """One pass over the bag grid; every protection applies per cell."""
        cfg = self.ctx.settings.sell
        whitelist = self._load_whitelist()
        money_before = self._money_crop()
        for page in range(cfg.max_pages):
            if page > 0:
                xy = self._sc.page_buttons[page]
                log.warning("using fallback coordinates", target=f"bag_page_{page + 1}", xy=xy)
                self.ctx.device.tap(*xy)
                time.sleep(1.0)
            self._scan_page(page, whitelist)
        self._verify_money_changed(money_before)
        log.info(
            "sell pass done",
            sold=self.report.sold,
            would_sell=self.report.would_sell,
            kept_unknown=self.report.skipped_unknown,
            kept_quality=self.report.skipped_quality,
        )
        return S_CLOSE

    def _close(self) -> str:
        self._close_shop_windows()
        if (
            self.ctx.vision.wait_any([TPL_HUD, TPL_BTN_SIT, TPL_BTN_STAND], timeout=15.0)
            is None
        ):
            raise StateTimeout("HUD did not come back after closing the shop")
        return DONE

    # -- internals ---------------------------------------------------------------

    def _scan_page(self, page: int, whitelist: dict[str, Image]) -> None:
        cfg = self.ctx.settings.sell
        grid = self._sc.bag_grid
        frame = self.ctx.vision.capture_screen()
        for row in range(grid.rows):
            for col in range(grid.cols):
                x = grid.origin[0] + col * grid.pitch[0]
                y = grid.origin[1] + row * grid.pitch[1]
                cell = frame[y : y + grid.cell[1], x : x + grid.cell[0]]
                if cell.size == 0:
                    continue
                # chỉ xét ô game TÔ MÀU "bán được" trong chế độ Bán
                if cell_highlight_fraction(cell, cfg) < cfg.highlight_min_fraction:
                    continue
                gray = cast(Image, cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY))
                name = best_whitelist_match(gray, whitelist, cfg.icon_match_threshold)
                detail = f"page{page + 1} cell({row},{col})"
                if name is None:
                    self.report.skipped_unknown += 1
                    continue
                quality = detect_quality(cell, self.ctx.settings.quality)
                if quality is not None and self._quality_index(quality) >= (
                    cfg.quality_blacklist_threshold
                ):
                    self.report.skipped_quality += 1
                    log.info("kept item: quality above threshold", cell=detail, quality=quality)
                    continue
                detail = f"{name} {detail}"
                if not self.ctx.safety.guard_irreversible("sell", detail, frame):
                    self.report.would_sell += 1
                    self.report.details.append(f"WOULD SELL {detail}")
                    continue
                self.ctx.device.tap(x + grid.cell[0] // 2, y + grid.cell[1] // 2)
                self.report.sold += 1
                self.report.details.append(f"SOLD {detail}")
                time.sleep(0.4)
                # chụp lại sau mỗi lần bán — lưới đã thay đổi
                frame = self.ctx.vision.capture_screen()

    def _whitelist_names(self) -> list[str]:
        return [n for n in self.ctx.vision.store.names if n.startswith(WHITELIST_PREFIX)]

    def _load_whitelist(self) -> dict[str, Image]:
        return {n: self.ctx.vision.store.get(n).gray for n in self._whitelist_names()}

    def _quality_index(self, quality: str) -> int:
        """Position of a quality name in the configured ladder (0=trắng...)."""
        names = list(self.ctx.settings.quality.colors)
        return names.index(quality) if quality in names else len(names)

    def _money_crop(self) -> Image:
        x, y, w, h = self._sc.money_rect
        return self.ctx.vision.capture_screen()[y : y + h, x : x + w].copy()

    def _verify_money_changed(self, before: Image) -> None:
        """After real sales the money row MUST have changed pixels."""
        if self.report.sold == 0:
            return
        after = self._money_crop()
        diff = float(cv2.absdiff(before, after).mean())
        if diff < 0.5:
            log.warning("money display did not change after selling", sold=self.report.sold)
        else:
            log.info("money display changed", sold=self.report.sold)

    def _close_shop_windows(self) -> None:
        """Close the shop window and the bag it opened alongside."""
        btn = self.ctx.vision.find(TPL_BTN_CLOSE_SHOP)
        if btn is not None:
            self.ctx.device.tap(*btn.center)
            self.ctx.vision.wait_vanish(TPL_SHOP_HEADER, timeout=5.0)
        # túi mở kèm shop — X của túi là common/btn_close (ROI đã khoá đúng vị trí)
        if "common/btn_close" in self.ctx.vision.store:
            bag_x = self.ctx.vision.find("common/btn_close")
            if bag_x is not None:
                self.ctx.device.tap(*bag_x.center)
