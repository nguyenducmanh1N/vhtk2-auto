"""R1: log into the game and land in the world (PLAN.md 4.1).

Discovered flow (AVD, game v1.0.32):

    TITLE ("Đăng nhập") -> SIGN IN dialog (3CID / Play Now) -> 3CID form
    (credentials may be pre-saved by the SDK) -> Sign In -> character select
    ("Chọn nhân vật") -> "Vào game" -> in-game HUD.

Design notes:
- The task NEVER taps "Play Now": that silently creates a brand-new guest
  account. Only the 3CID path is automated.
- The task NEVER creates characters: landing on the character-CREATION screen
  means this account has no character on the selected server -> ABORT with a
  clear message; a human creates the character once, by hand.
- Empty ``username``/``password`` in the account config means "use the
  credentials saved in the SDK form" (Save Account Information) — the task
  just presses Sign In.
- Captcha/verification screens have no templates on purpose: they fall through
  recovery's unknown-screen path (evidence + notify + eventual ABORT), per the
  project rule that humans handle verification.
- Every screen the flow may land on is detected with ``wait_any`` so the task
  is idempotent: started from ANY point of the flow, it routes itself forward.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from vhtk.config import Coordinates, load_coordinates
from vhtk.core.errors import StateTimeout, TaskDefinitionError
from vhtk.core.fsm import ABORT, DONE, BaseTask, State, TaskContext

log = structlog.get_logger(__name__)

# Tên template của flow login (UI contract với bộ template đang dùng)
TPL_BTN_ENTER = "login/btn_enter"  # nút "Đăng nhập" ở màn title
# hộp "Không lấy được cấu hình cập nhật. Thử lại?" khi mạng yếu lúc khởi động
# — bấm "Thử lại" tới khi qua được (quan sát 26/10 khi mạng chập chờn)
TPL_BTN_RETRY_CONFIG = "login/btn_retry_config"
TPL_SERVER_ROW = "login/server_row"  # dòng server đã chọn đúng (khớp = đúng server)
TPL_SIGNIN_TITLE = "login/signin_title"  # dialog "SIGN IN" của SDK
TPL_BTN_3CID = "login/btn_3cid"
TPL_FORM_TITLE = "login/form_3cid_title"  # form "SIGN IN WITH 3CID"
TPL_BTN_SIGN_IN = "login/btn_sign_in"
TPL_CHAR_SELECT = "login/char_select_header"  # màn "Chọn nhân vật"
TPL_CHAR_CREATE = "login/char_create_header"  # màn TẠO nhân vật -> ABORT
TPL_BTN_ENTER_GAME = "login/btn_enter_game"  # nút "Vào game"
TPL_HUD = "login/hud_auto_btn"  # nút Auto tròn trên HUD = đã vào thế giới

# State names
S_LAUNCH = "LAUNCH_APP"
S_WAIT_TITLE = "WAIT_TITLE"
S_CHECK_SERVER = "CHECK_SERVER"
S_TAP_ENTER = "TAP_ENTER"
S_SIGN_IN = "SIGN_IN_DIALOG"
S_FILL_FORM = "FILL_FORM"
S_SUBMIT = "SUBMIT"
S_SELECT_CHAR = "SELECT_CHARACTER"
S_VERIFY = "VERIFY_INGAME"

# Màn nào của flow cũng có thể là điểm đáp sau một bước -> state xử lý nó
_LANDING_STATES: dict[str, str] = {
    TPL_SIGNIN_TITLE: S_SIGN_IN,
    TPL_FORM_TITLE: S_FILL_FORM,
    TPL_CHAR_SELECT: S_SELECT_CHAR,
    TPL_CHAR_CREATE: ABORT,
    TPL_HUD: S_VERIFY,
}


class LoginTask(BaseTask):
    """Log the configured account in, from any starting screen of the flow."""

    name = "login"
    # màn login là "sân nhà" của task này — recovery không được đá ra ngoài
    relogin_is_normal = True

    def __init__(self, ctx: TaskContext) -> None:
        if ctx.account is None:
            raise TaskDefinitionError("LoginTask requires ctx.account")
        self._account = ctx.account
        self._coords: Coordinates = load_coordinates(ctx.settings.paths.coordinates_file)
        super().__init__(ctx)

    def states(self) -> Sequence[State]:
        cfg = self.ctx.settings.login
        landing = set(_LANDING_STATES.values()) - {ABORT}
        return [
            State(S_LAUNCH, self._launch_app, 30.0, frozenset({S_WAIT_TITLE})),
            State(
                S_WAIT_TITLE,
                self._wait_title,
                cfg.boot_timeout,
                frozenset({S_CHECK_SERVER} | landing),
            ),
            State(S_CHECK_SERVER, self._check_server, 15.0, frozenset({S_TAP_ENTER})),
            State(S_TAP_ENTER, self._tap_enter, cfg.flow_timeout, frozenset(landing)),
            State(S_SIGN_IN, self._sign_in_dialog, cfg.flow_timeout, frozenset({S_FILL_FORM})),
            State(S_FILL_FORM, self._fill_form, 60.0, frozenset({S_SUBMIT})),
            State(
                S_SUBMIT,
                self._submit,
                cfg.flow_timeout,
                frozenset({S_SELECT_CHAR, S_VERIFY}),
            ),
            State(S_SELECT_CHAR, self._select_character, cfg.flow_timeout, frozenset({S_VERIFY})),
            State(S_VERIFY, self._verify_ingame, cfg.ingame_timeout, frozenset({DONE})),
        ]

    def initial_state(self) -> str:
        return S_LAUNCH

    def recovery_resume_state(self, failed_state: str) -> str:
        """After any recovery, resume at WAIT_TITLE: the screen may have moved
        anywhere in the flow (e.g. network-error popup dropped us back to the
        title), and WAIT_TITLE routes itself to whatever screen is showing."""
        return S_WAIT_TITLE

    # -- helpers ---------------------------------------------------------------

    def _tap_template(self, template: str, timeout: float) -> None:
        """Find a template and tap its center; StateTimeout if absent."""
        m = self.ctx.vision.wait_for(template, timeout=timeout)
        if m is None:
            raise StateTimeout(f"{template} not found within {timeout}s")
        self.ctx.device.tap(*m.center)

    def _tap_fallback_coords(self, what: str, xy: tuple[int, int]) -> None:
        """Tap a hard-coded coordinate — allowed only where no template can
        exist (text input fields); must warn on every use (rule #1)."""
        log.warning("using fallback coordinates", target=what, xy=xy)
        self.ctx.device.tap(*xy)

    def _route_landing(self, found: str) -> str:
        nxt = _LANDING_STATES[found]
        if nxt == ABORT:
            log.error(
                "landed on character-creation screen: account has no character "
                "on this server — create it by hand once",
                account=self._account.name,
                server=self._account.login.server,
            )
        return nxt

    # -- state handlers ----------------------------------------------------------

    def _ensure_foreground(self) -> None:
        """Bring the game to the foreground if it is not the focused window.

        Process-alive is NOT enough: the game can crash back to the launcher
        while its process survives — ``monkey`` both starts and refocuses it.
        """
        package = self.ctx.settings.game.package_name
        if not self.ctx.device.app_in_foreground(package):
            log.warning("game not in foreground, (re)starting", package=package)
            self.ctx.device.start_app(package)

    def _launch_app(self) -> str:
        self._ensure_foreground()
        return S_WAIT_TITLE

    def _wait_title(self) -> str:
        """Wait for the title screen OR any later screen of the flow."""
        # retry sau RECOVER quay lại đây — tự kéo game lên foreground nếu nó
        # đã văng về launcher trong lúc chờ
        self._ensure_foreground()
        cfg = self.ctx.settings.login
        watch = [TPL_BTN_ENTER, *_LANDING_STATES]
        if TPL_BTN_RETRY_CONFIG in self.ctx.vision.store:
            watch.append(TPL_BTN_RETRY_CONFIG)
        # hộp "cấu hình cập nhật" có thể hiện lại nhiều lần khi mạng yếu —
        # bấm Thử lại tối đa vài lần trong cùng một lượt chờ
        for _ in range(3):
            hit = self.ctx.vision.wait_any(watch, timeout=cfg.boot_timeout)
            if hit is None:
                raise StateTimeout("no known login screen appeared after app start")
            found, match = hit
            if found == TPL_BTN_RETRY_CONFIG:
                log.warning("config-update dialog showing, tapping retry")
                self.ctx.device.tap(*match.center)
                continue
            if found == TPL_BTN_ENTER:
                return S_CHECK_SERVER
            return self._route_landing(found)
        raise StateTimeout("config-update dialog keeps reappearing (network down?)")

    def _check_server(self) -> str:
        """Verify the pre-selected server row matches the account's server.

        ``login/server_row`` is a crop of the WHOLE selected-server row for
        this account; a match means the right server is selected. Missing
        template (fresh template set) just warns — OCR comparison arrives
        with the Windows/PaddleOCR environment.
        """
        if TPL_SERVER_ROW not in self.ctx.vision.store:
            log.warning("server_row template missing, skipping server check")
            return S_TAP_ENTER
        if self.ctx.vision.find(TPL_SERVER_ROW) is None:
            log.error(
                "selected server does not match account config",
                expected=self._account.login.server,
            )
            return ABORT
        return S_TAP_ENTER

    def _tap_enter(self) -> str:
        cfg = self.ctx.settings.login
        self._tap_template(TPL_BTN_ENTER, timeout=10.0)
        hit = self.ctx.vision.wait_any(list(_LANDING_STATES), timeout=cfg.flow_timeout)
        if hit is None:
            raise StateTimeout("nothing appeared after tapping Đăng nhập")
        return self._route_landing(hit[0])

    def _sign_in_dialog(self) -> str:
        # template signin_title ("SIGN IN") match được cả header
        # "SIGN IN WITH 3CID" của form — mất phiên SDK là game rơi THẲNG về
        # form, lúc đó không có nút 3CID nào để bấm (lỗi thực tế 26/10)
        if self.ctx.vision.find(TPL_FORM_TITLE) is not None:
            log.info("3CID form already open, skipping method chooser")
            return S_FILL_FORM
        self._tap_template(TPL_BTN_3CID, timeout=10.0)
        if self.ctx.vision.wait_for(TPL_FORM_TITLE, timeout=15.0) is None:
            raise StateTimeout("3CID form did not open")
        return S_FILL_FORM

    def _fill_form(self) -> str:
        """Type credentials from config; empty config = use SDK-saved ones."""
        creds = self._account.login
        if not creds.username:
            log.info("no credentials in config, using SDK-saved account")
            return S_SUBMIT
        cfg = self.ctx.settings.login
        coords = self._coords.login
        # landscape + IME ảo: tap vào ô là Android mở editor TOÀN MÀN HÌNH
        # che hết form (lỗi thực tế 26/10) — press_back sau mỗi lần gõ để
        # đóng editor/bàn phím trước khi đụng phần tử kế tiếp. Trên AVD dev
        # đã tắt IME ảo (show_ime_with_hard_keyboard=0), back thừa vô hại.
        self._tap_fallback_coords("username_field", coords.username_field)
        self.ctx.device.press_delete(cfg.clear_chars)
        self.ctx.device.input_text(creds.username)
        self.ctx.device.press_back()
        self._tap_fallback_coords("password_field", coords.password_field)
        self.ctx.device.press_delete(cfg.clear_chars)
        self.ctx.device.input_text(creds.password)
        self.ctx.device.press_back()
        return S_SUBMIT

    def _submit(self) -> str:
        cfg = self.ctx.settings.login
        self._tap_template(TPL_BTN_SIGN_IN, timeout=10.0)
        hit = self.ctx.vision.wait_any(
            [TPL_CHAR_SELECT, TPL_CHAR_CREATE, TPL_HUD], timeout=cfg.flow_timeout
        )
        if hit is None:
            # sai mật khẩu / mạng chậm — form thường vẫn còn trên màn
            raise StateTimeout("sign-in did not reach character select")
        return self._route_landing(hit[0])

    def _select_character(self) -> str:
        slots = self._coords.login.char_slots
        slot = self._account.login.character_slot
        if slot > len(slots):
            log.error("character_slot out of range", slot=slot, available=len(slots))
            return ABORT
        self._tap_fallback_coords(f"char_slot_{slot}", slots[slot - 1])
        self._tap_template(TPL_BTN_ENTER_GAME, timeout=10.0)
        return S_VERIFY

    def _verify_ingame(self) -> str:
        cfg = self.ctx.settings.login
        if self.ctx.vision.wait_for(TPL_HUD, timeout=cfg.ingame_timeout) is None:
            raise StateTimeout("HUD did not appear after entering the world")
        log.info("login complete", account=self._account.name)
        return DONE
