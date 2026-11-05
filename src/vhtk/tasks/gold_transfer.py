"""R4: hand the farmed money to the main account by direct trade (PLAN.md W3).

Two characters must stand next to each other (confirmed in-game: no mail
gold). Two cooperating FSMs, one per side, synchronized through a small
barrier file in ``orchestrator.workers_dir`` (written atomically, polled):

    farm side : VERIFY_HUD -> GO_RENDEZVOUS -> WAIT_PARTNER -> INVITE
                -> PUT_MONEY -> LOCK -> CONFIRM -> VERIFY_AFTER -> DONE
    main side : VERIFY_HUD -> GO_RENDEZVOUS -> WAIT_PARTNER -> ACCEPT
                -> LOCK -> CONFIRM -> VERIFY_AFTER -> DONE

Invite flow VERIFIED on the AVD (2026-11-05): tap the partner's sprite ->
target plate appears at the top of the HUD -> tap the NAME on that plate ->
player menu (Tán gẫu / Hảo hữu / Giao dịch / Tổ đội / Theo sau / Tin tức /
Sổ đen / Tỷ võ) -> tap "Giao dịch". Tapping the plate avatar only deselects;
tapping a name in chat gives a reduced menu without "Giao dịch".

Trade-window flow is still assumed from the PC original (JX2): each side
puts its offer, presses "Khoá", then "Xác nhận"; the window closes on
success. Every template under ``trade/`` is a UI CONTRACT to be captured
with 2 accounts — until then VERIFY_HUD refuses to run (missing templates /
unmeasured coordinates), never taps blind.

Safety: PUT_MONEY and CONFIRM go through ``guard_irreversible`` — dry_run
stops right before the game-side confirmation; a human can approve via
confirm_mode. Success is only claimed when OCR sees the money move on BOTH
screens (``transfer_verified``).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from vhtk.config import Coordinates, TradeCoordinates, load_coordinates
from vhtk.core.errors import StateTimeout, TaskDefinitionError, TaskError, VerifyFailed
from vhtk.core.fsm import ABORT, DONE, BaseTask, State, TaskContext
from vhtk.core.vision import Image
from vhtk.mapstore import MapStore, maps_dir
from vhtk.tasks.farm import TPL_HUD
from vhtk.tasks.money import Money, MoneyReader, transfer_amount, transfer_verified
from vhtk.tasks.navigate import (
    TPL_BTN_CONFIRM_COORDS,
    TPL_BTN_SIT,
    TPL_BTN_STAND,
    Navigator,
)

log = structlog.get_logger(__name__)

# -- UI contract (templates/<version>/trade/) ------------------------------------
TPL_MENU_TRADE = "trade/menu_giao_dich"  # nút "Giao dịch" trong menu người chơi
TPL_INVITE_ACCEPT = "trade/btn_dong_y"  # nút đồng ý trong popup "X mời giao dịch"
TPL_WINDOW = "trade/window_header"  # tiêu đề khung giao dịch đang mở
TPL_BTN_LOCK = "trade/btn_khoa"  # nút Khoá (bên mình)
TPL_LOCKED = "trade/locked_mark"  # dấu đã khoá (bên mình) sau khi bấm
TPL_PARTNER_LOCKED = "trade/partner_locked_mark"  # dấu bên kia đã khoá
TPL_BTN_CONFIRM = "trade/btn_xac_nhan"  # nút Xác nhận
TPL_BTN_CANCEL = "trade/btn_huy"  # nút Huỷ giao dịch
TPL_SUCCESS = "trade/success_notice"  # thông báo giao dịch thành công
TPL_BTN_BAG = "common/btn_bag"  # nút Túi đồ trên HUD (đọc tiền trước/sau giao dịch)

Side = Literal["farm", "main"]
Phase = Literal["", "arrived", "invited", "offered", "locked", "confirmed", "done", "failed"]
PHASE_ORDER: tuple[str, ...] = (
    "",
    "arrived",
    "invited",
    "offered",
    "locked",
    "confirmed",
    "done",
    "failed",
)

S_VERIFY_HUD = "VERIFY_HUD"
S_GO_RENDEZVOUS = "GO_RENDEZVOUS"
S_WAIT_PARTNER = "WAIT_PARTNER"
S_INVITE = "INVITE"
S_ACCEPT = "ACCEPT"
S_PUT_MONEY = "PUT_MONEY"
S_LOCK = "LOCK"
S_CONFIRM = "CONFIRM"
S_VERIFY_AFTER = "VERIFY_AFTER"
S_CANCEL = "CANCEL"


# -- barrier file -----------------------------------------------------------------


def barrier_file(workers_dir: Path) -> Path:
    return workers_dir / "transfer.json"


@dataclass
class Barrier:
    """Shared trade session state, one JSON file, atomic rewrites.

    Fields per side: ``phase`` (see PHASE_ORDER), ``money_before``/``money_after``
    (the traded currency as OCR'd), ``amount`` (farm's intended transfer).
    The coordinator creates the file with ``farm``/``main`` stems and a
    ``session`` id; both tasks refuse to run on a file naming another stem.
    """

    path: Path

    def read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def update_side(self, side: Side, **fields: Any) -> None:
        data = self.read()
        entry = dict(data.get(side) or {})
        entry.update(fields)
        entry["updated_at"] = time.time()
        data[side] = entry
        self.write(data)

    def phase(self, side: Side) -> str:
        entry = self.read().get(side) or {}
        return str(entry.get("phase", "")) if isinstance(entry, dict) else ""

    def side_value(self, side: Side, key: str) -> Any:
        entry = self.read().get(side) or {}
        return entry.get(key) if isinstance(entry, dict) else None


def phase_at_least(phase: str, target: str) -> bool:
    """True when ``phase`` is ``target`` or later in PHASE_ORDER (failed counts as never)."""
    if phase == "failed" or target not in PHASE_ORDER or phase not in PHASE_ORDER:
        return False
    return PHASE_ORDER.index(phase) >= PHASE_ORDER.index(target)


def other(side: Side) -> Side:
    return "main" if side == "farm" else "farm"


# -- shared task body ------------------------------------------------------------


@dataclass
class TransferReport:
    side: Side
    amount: int = 0
    before: Money = field(default_factory=Money)
    after: Money = field(default_factory=Money)
    verified: bool = False
    would_transfer: bool = False  # dry_run / confirm từ chối -> dừng trước xác nhận
    details: list[str] = field(default_factory=list)


class _TransferBase(BaseTask):
    """Everything both sides share: rendezvous, barrier, lock/confirm/verify."""

    side: Side = "farm"

    def __init__(self, ctx: TaskContext) -> None:
        if ctx.account is None:
            raise TaskDefinitionError(f"{type(self).__name__} requires ctx.account")
        self.cfg = ctx.settings.transfer
        coords: Coordinates = load_coordinates(ctx.settings.paths.coordinates_file)
        self._tc: TradeCoordinates = coords.trade
        store = MapStore(maps_dir(ctx.settings.templates.version))
        self._nav = Navigator(ctx, coords.farm, ctx.settings.farm, store=store)
        self._money = MoneyReader(coords.sell.money_cells, None, ctx.settings.ocr.lang)
        self._barrier = Barrier(barrier_file(ctx.settings.orchestrator.workers_dir))
        self._nav_attempts = 0
        self.report = TransferReport(side=self.side)
        super().__init__(ctx)

    # -- graph pieces ---------------------------------------------------------------

    def _common_states(self, after_wait: str) -> list[State]:
        cfg = self.ctx.settings.farm
        return [
            State(S_VERIFY_HUD, self._verify_hud, 30.0, frozenset({S_GO_RENDEZVOUS})),
            State(
                S_GO_RENDEZVOUS,
                self._go_rendezvous,
                cfg.map_timeout * 3 + cfg.travel_timeout,
                frozenset({S_WAIT_PARTNER, S_GO_RENDEZVOUS}),
            ),
            State(
                S_WAIT_PARTNER,
                self._wait_partner,
                self.cfg.partner_wait_timeout,
                frozenset({after_wait, S_CANCEL}),
            ),
            State(
                S_LOCK,
                self._lock,
                self.cfg.trade_step_timeout * 2,
                frozenset({S_CONFIRM, S_CANCEL}),
            ),
            State(
                S_CONFIRM,
                self._confirm,
                self.cfg.trade_step_timeout * 2,
                frozenset({S_VERIFY_AFTER, S_CANCEL}),
            ),
            State(S_VERIFY_AFTER, self._verify_after, 30.0, frozenset({DONE})),
            State(S_CANCEL, self._cancel, self.cfg.trade_step_timeout, frozenset({DONE})),
        ]

    def initial_state(self) -> str:
        return S_VERIFY_HUD

    def recovery_resume_state(self, failed_state: str) -> str:
        """Screen may be anywhere after recovery; the barrier remembers progress."""
        return S_VERIFY_HUD

    def _required_templates(self) -> tuple[str, ...]:
        return (
            TPL_WINDOW,
            TPL_BTN_LOCK,
            TPL_BTN_CONFIRM,
            TPL_BTN_CANCEL,
            TPL_BTN_CONFIRM_COORDS,
            TPL_BTN_BAG,
        )

    # -- handlers -------------------------------------------------------------------

    def _verify_hud(self) -> str:
        self.report = TransferReport(side=self.side)
        problems: list[str] = []
        if not self.cfg.rendezvous_map:
            problems.append("transfer.rendezvous_map not configured")
        problems += self._nav.missing_templates(*self._required_templates())
        problems += self._nav.map_requirements_missing(self.cfg.rendezvous_map)
        problems += [f"coordinates.trade.{n} not measured" for n in self._tc.unmeasured()]
        if not self._money.available:
            problems.append("money OCR unavailable (sell.money_cells / paddleocr)")
        session = self._barrier.read()
        if session.get(self.side + "_stem") != self.ctx.safety.stem:
            problems.append(f"barrier file does not name this account as {self.side}")
        if problems:
            log.error("transfer prerequisites missing", side=self.side, problems=problems)
            self._barrier.update_side(self.side, phase="failed", reason="; ".join(problems))
            return ABORT
        hit = self.ctx.vision.wait_any(
            [TPL_HUD, TPL_BTN_SIT, TPL_BTN_STAND, TPL_BTN_CONFIRM_COORDS, TPL_WINDOW],
            timeout=20.0,
        )
        if hit is None:
            raise StateTimeout("neither HUD, map nor trade window is showing")
        if hit[0] == TPL_WINDOW:
            # khung giao dịch còn mở từ lần trước — huỷ cho sạch rồi làm lại
            self._tap_template(TPL_BTN_CANCEL, "cancel stale trade window")
            self.ctx.vision.wait_vanish(TPL_WINDOW, timeout=self.cfg.trade_step_timeout)
        elif hit[0] == TPL_BTN_CONFIRM_COORDS:
            self._nav.close_map()
        return S_GO_RENDEZVOUS

    def _go_rendezvous(self) -> str:
        cfg = self.ctx.settings.farm
        here = self._nav.in_map(self.cfg.rendezvous_map)
        if here is None:
            here = self._nav.confirm_in_map(self.cfg.rendezvous_map)
        self._nav.ensure_standing()
        self._nav.open_map()
        if not here:
            self._nav.select_map(self.cfg.rendezvous_map)
        x, y = self._nav.resolve_spot(self.cfg.rendezvous_spot)
        try:
            self._nav.tap_map_point(x, y)
        except VerifyFailed:
            if here:
                log.info("already at rendezvous map, standing in place", side=self.side)
                self._nav.close_map()
                return S_WAIT_PARTNER
            raise
        if not self._nav.travel_started(cfg.travel_start_timeout):
            self._nav_attempts += 1
            if self._nav_attempts > cfg.nav_retries:
                log.error("pathfinding never started, giving up", attempts=self._nav_attempts)
                self._barrier.update_side(self.side, phase="failed", reason="no pathfinding")
                return ABORT
            return S_GO_RENDEZVOUS
        self._nav.wait_arrival(self.cfg.rendezvous_map)
        self._nav_attempts = 0
        return S_WAIT_PARTNER

    def _wait_partner(self) -> str:
        raise NotImplementedError  # each side overrides

    def _arrive_and_wait(self, next_state: str, partner_phase: str) -> str:
        """Mark arrived (with money_before) and wait for the partner's phase."""
        before = self._read_money_via_bag()
        self.report.before = before
        self._barrier.update_side(
            self.side,
            phase="arrived",
            money_before=before.get(self.cfg.currency),
        )
        deadline = time.monotonic() + self.cfg.partner_wait_timeout
        partner = other(self.side)
        while time.monotonic() < deadline:
            self.ctx.safety.check_kill_switch()
            phase = self._barrier.phase(partner)
            if phase == "failed":
                log.warning("partner failed, cancelling", side=self.side)
                return S_CANCEL
            if phase_at_least(phase, partner_phase):
                return next_state
            time.sleep(2.0)
        log.error("partner never arrived", side=self.side, waited=self.cfg.partner_wait_timeout)
        self._barrier.update_side(self.side, phase="failed", reason="partner timeout")
        return S_CANCEL

    def _lock(self) -> str:
        if not self._wait_window():
            return S_CANCEL
        self._tap_template(TPL_BTN_LOCK, "lock offer")
        if TPL_LOCKED in self.ctx.vision.store and (
            self.ctx.vision.wait_for(TPL_LOCKED, timeout=self.cfg.trade_step_timeout) is None
        ):
            raise VerifyFailed("lock mark did not appear")
        self._barrier.update_side(self.side, phase="locked")
        if not self._await_partner_phase("locked"):
            return S_CANCEL
        if TPL_PARTNER_LOCKED in self.ctx.vision.store and (
            self.ctx.vision.wait_for(TPL_PARTNER_LOCKED, timeout=self.cfg.trade_step_timeout)
            is None
        ):
            raise VerifyFailed("partner lock mark not visible on screen")
        return S_CONFIRM

    def _confirm(self) -> str:
        if not self._wait_window():
            return S_CANCEL
        frame = self.ctx.vision.capture_screen()
        detail = f"{self.side} confirm trade amount={self.report.amount} {self.cfg.currency}"
        if not self.ctx.safety.guard_irreversible("trade_confirm", detail, frame):
            self.report.would_transfer = True
            self.report.details.append(f"WOULD CONFIRM {detail}")
            self._barrier.update_side(self.side, phase="failed", reason="dry_run/denied")
            return S_CANCEL
        self._tap_template(TPL_BTN_CONFIRM, "confirm trade")
        self._barrier.update_side(self.side, phase="confirmed")
        if not self._await_partner_phase("confirmed"):
            return S_CANCEL
        # cả hai đã xác nhận — khung tự đóng (hoặc thông báo thành công)
        if self.ctx.vision.wait_vanish(TPL_WINDOW, timeout=self.cfg.trade_step_timeout * 2):
            return S_VERIFY_AFTER
        if TPL_SUCCESS in self.ctx.vision.store and (self.ctx.vision.find(TPL_SUCCESS) is not None):
            return S_VERIFY_AFTER
        raise VerifyFailed("trade window still open after both confirmed")

    def _verify_after(self) -> str:
        after = self._read_money_via_bag()
        self.report.after = after
        self._barrier.update_side(self.side, phase="done", money_after=after.get(self.cfg.currency))
        # chờ bên kia ghi money_after rồi đối chiếu CẢ HAI phía
        partner = other(self.side)
        deadline = time.monotonic() + self.cfg.trade_step_timeout * 2
        while time.monotonic() < deadline and not phase_at_least(
            self._barrier.phase(partner), "done"
        ):
            time.sleep(1.0)
        farm_b = self._barrier.side_value("farm", "money_before")
        farm_a = self._barrier.side_value("farm", "money_after")
        main_b = self._barrier.side_value("main", "money_before")
        main_a = self._barrier.side_value("main", "money_after")
        amount = self._barrier.side_value("farm", "amount")
        ok = isinstance(amount, int) and transfer_verified(
            farm_b, farm_a, main_b, main_a, amount, self.cfg.verify_tolerance
        )
        self.report.verified = ok
        self._barrier.update_side(self.side, verified=ok)
        if ok:
            log.info("transfer verified", side=self.side, amount=amount)
            self.ctx.notifier.send_text(
                f"[{self.ctx.safety.stem}] transfer verified: {amount} {self.cfg.currency}"
            )
        else:
            log.error(
                "transfer NOT verified — money mismatch",
                side=self.side,
                farm=(farm_b, farm_a),
                main=(main_b, main_a),
                amount=amount,
            )
            self.ctx.notifier.send_photo(
                self.ctx.vision.capture_screen(),
                f"[{self.ctx.safety.stem}] transfer mismatch farm={farm_b}->{farm_a} "
                f"main={main_b}->{main_a} amount={amount}",
            )
        return DONE

    def _cancel(self) -> str:
        """Leave no trade window behind; mark failed unless already done."""
        if self._barrier.phase(self.side) not in ("done", "failed"):
            self._barrier.update_side(self.side, phase="failed", reason="cancelled")
        if self.ctx.vision.find(TPL_WINDOW) is not None:
            btn = self.ctx.vision.find(TPL_BTN_CANCEL)
            if btn is not None:
                self.ctx.device.tap(*btn.center)
                self.ctx.vision.wait_vanish(TPL_WINDOW, timeout=self.cfg.trade_step_timeout)
        return DONE

    # -- helpers ----------------------------------------------------------------------

    def _tap_template(self, name: str, what: str) -> None:
        m = self.ctx.vision.wait_for(name, timeout=self.cfg.trade_step_timeout)
        if m is None:
            raise StateTimeout(f"{what}: template {name} not found")
        self.ctx.device.tap(*m.center)

    def _wait_window(self) -> bool:
        if self.ctx.vision.wait_for(TPL_WINDOW, timeout=self.cfg.trade_step_timeout) is None:
            log.warning("trade window not open", side=self.side)
            return False
        return True

    def _await_partner_phase(self, phase: str) -> bool:
        partner = other(self.side)
        deadline = time.monotonic() + self.cfg.trade_step_timeout * 2
        while time.monotonic() < deadline:
            self.ctx.safety.check_kill_switch()
            p = self._barrier.phase(partner)
            if p == "failed":
                return False
            if phase_at_least(p, phase):
                return True
            time.sleep(1.0)
        log.warning("partner did not reach phase in time", side=self.side, phase=phase)
        return False

    def _read_money_via_bag(self) -> Money:
        """Open the bag, OCR the money row, close the bag.

        Túi mở bằng nút túi trên HUD (template common/btn_bag) — nếu thiếu
        template thì trả Money() rỗng (xác minh sẽ thất bại, không tap mù).
        """
        if TPL_BTN_BAG not in self.ctx.vision.store:
            log.warning("bag button template missing — cannot read money", template=TPL_BTN_BAG)
            return Money()
        self._tap_template(TPL_BTN_BAG, "open bag")
        time.sleep(1.0)
        frame: Image = self.ctx.vision.capture_screen()
        money = self._money.read(frame)
        close = self.ctx.vision.find("common/btn_close")
        if close is not None:
            self.ctx.device.tap(*close.center)
        return money


class TransferFarmTask(_TransferBase):
    """Farm side: invite, put ``balance - gold_keep`` on the table, lock, confirm."""

    name = "transfer_farm"
    side = "farm"

    def states(self) -> Sequence[State]:
        extra = [
            State(
                S_INVITE,
                self._invite,
                self.cfg.trade_step_timeout * 2,
                frozenset({S_PUT_MONEY, S_CANCEL}),
            ),
            State(
                S_PUT_MONEY,
                self._put_money,
                self.cfg.trade_step_timeout * 2,
                frozenset({S_LOCK, S_CANCEL}),
            ),
        ]
        return self._common_states(after_wait=S_INVITE) + extra

    def _required_templates(self) -> tuple[str, ...]:
        return (*super()._required_templates(), TPL_MENU_TRADE)

    def _wait_partner(self) -> str:
        return self._arrive_and_wait(S_INVITE, "arrived")

    def _invite(self) -> str:
        # chọn mục tiêu (bảng mục tiêu hiện ở đỉnh màn) rồi tap TÊN trên bảng
        # mới ra menu người chơi — bảng không có template (tên động) nên 2 cú
        # tap đầu là toạ độ, xác minh bằng nút "Giao dịch" xuất hiện
        log.warning("using fallback coordinates", target="partner_tap", xy=self._tc.partner_tap)
        self.ctx.device.tap(*self._tc.partner_tap)
        time.sleep(1.0)
        log.warning(
            "using fallback coordinates", target="plate_name_tap", xy=self._tc.plate_name_tap
        )
        self.ctx.device.tap(*self._tc.plate_name_tap)
        self._tap_template(TPL_MENU_TRADE, "trade menu entry")
        self._barrier.update_side(self.side, phase="invited")
        if not self._wait_window():
            return S_CANCEL
        return S_PUT_MONEY

    def _put_money(self) -> str:
        assert self.ctx.account is not None
        balance = self.report.before.get(self.cfg.currency)
        if balance is None:
            log.error("balance unreadable, refusing to trade")
            return S_CANCEL
        amount = transfer_amount(balance, self.ctx.account.farm.gold_keep)
        self.report.amount = amount
        if amount <= 0:
            log.warning("nothing to transfer after gold_keep", balance=balance)
            return S_CANCEL
        log.warning("using fallback coordinates", target="money_input", xy=self._tc.money_input)
        self.ctx.device.tap(*self._tc.money_input)
        self.ctx.device.input_text(str(amount))
        # xác minh ô "mình đặt" hiện đúng số
        x, y, w, h = self._tc.own_offer_rect
        shown = self._money_reader_number(self.ctx.vision.capture_screen()[y : y + h, x : x + w])
        if shown != amount:
            raise VerifyFailed(f"offer field shows {shown!r}, expected {amount}")
        self._barrier.update_side(self.side, phase="offered", amount=amount)
        return S_LOCK

    def _money_reader_number(self, roi: Image) -> int | None:
        engine = self._money._engine()
        return engine.read_number(roi) if engine is not None else None


class TransferMainTask(_TransferBase):
    """Main side: accept the invite, verify the offer, lock, confirm."""

    name = "transfer_main"
    side = "main"

    def states(self) -> Sequence[State]:
        extra = [
            State(
                S_ACCEPT,
                self._accept,
                self.cfg.trade_step_timeout * 2,
                frozenset({S_LOCK, S_CANCEL}),
            ),
        ]
        return self._common_states(after_wait=S_ACCEPT) + extra

    def _required_templates(self) -> tuple[str, ...]:
        return (*super()._required_templates(), TPL_INVITE_ACCEPT)

    def _wait_partner(self) -> str:
        return self._arrive_and_wait(S_ACCEPT, "invited")

    def _accept(self) -> str:
        self._tap_template(TPL_INVITE_ACCEPT, "accept trade invite")
        if not self._wait_window():
            return S_CANCEL
        if not self._await_partner_phase("offered"):
            return S_CANCEL
        amount = self._barrier.side_value("farm", "amount")
        x, y, w, h = self._tc.partner_offer_rect
        engine = self._money._engine()
        shown = (
            engine.read_number(self.ctx.vision.capture_screen()[y : y + h, x : x + w])
            if engine is not None
            else None
        )
        if not isinstance(amount, int) or shown != amount:
            raise TaskError(f"partner offer shows {shown!r}, barrier says {amount!r}")
        self.report.amount = amount
        return S_LOCK
