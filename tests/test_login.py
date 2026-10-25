"""Tests for the login task (R1): routing, branching, abort conditions.

Runs entirely on fakes — the scripted vision answers wait_any/wait_for/find
in call order, the fake device records every tap/keystroke.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest

from vhtk.config import (
    AccountConfig,
    Settings,
    load_settings,
)
from vhtk.core.device import Device
from vhtk.core.errors import TaskDefinitionError
from vhtk.core.fsm import TaskContext, TaskStatus
from vhtk.core.recovery import Recovery, RecoveryOutcome
from vhtk.core.safety import Safety
from vhtk.core.vision import Match, Vision
from vhtk.notify import NullNotifier
from vhtk.tasks import login as login_mod
from vhtk.tasks.login import LoginTask

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
REPO_ROOT = Path(__file__).parent.parent


def match_at(x: int = 100, y: int = 100) -> Match:
    return Match(bbox=(x, y, 40, 20), score=0.99)


class FakeStore:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def __contains__(self, name: str) -> bool:
        return name in self.names


class ScriptedVision:
    """Answers wait_any / wait_for / find from per-method queues."""

    def __init__(
        self,
        wait_any: list[tuple[str, Match] | None] | None = None,
        wait_for: list[Match | None] | None = None,
        find: list[Match | None] | None = None,
        store_names: set[str] | None = None,
    ) -> None:
        self.wait_any_q = deque(wait_any or [])
        self.wait_for_q = deque(wait_for or [])
        self.find_q = deque(find or [])
        self.store = FakeStore(store_names if store_names is not None else {"login/server_row"})
        self.wait_any_calls: list[list[str]] = []
        self.wait_for_calls: list[str] = []

    def wait_any(
        self, names: Sequence[str], timeout: float | None = None, interval: float | None = None
    ) -> tuple[str, Match] | None:
        self.wait_any_calls.append(list(names))
        result = self.wait_any_q.popleft() if self.wait_any_q else None
        if result is not None:
            assert result[0] in names, f"scripted {result[0]} not in asked {names}"
        return result

    def wait_for(
        self, name: str, timeout: float | None = None, interval: float | None = None
    ) -> Match | None:
        self.wait_for_calls.append(name)
        return self.wait_for_q.popleft() if self.wait_for_q else None

    def find(self, name: str, screenshot: object = None, threshold: object = None) -> Match | None:
        return self.find_q.popleft() if self.find_q else None

    def capture_screen(self) -> object:
        import numpy as np

        return np.zeros((10, 10, 3), dtype=np.uint8)


class FakeDevice:
    def __init__(self, app_running: bool = False) -> None:
        self._app_running = app_running
        self.taps: list[tuple[int, int]] = []
        self.texts: list[str] = []
        self.deletes: list[int] = []
        self.started: list[str] = []

    def app_is_running(self, package: str) -> bool:
        return self._app_running

    def app_in_foreground(self, package: str) -> bool:
        return self._app_running

    def start_app(self, package: str) -> None:
        self.started.append(package)
        self._app_running = True

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def input_text(self, text: str) -> None:
        self.texts.append(text)

    def press_delete(self, count: int) -> None:
        self.deletes.append(count)


class OkRecovery:
    def check(self) -> RecoveryOutcome:
        return RecoveryOutcome.OK

    def on_state_failure(self, state: str, reason: str) -> RecoveryOutcome:
        return RecoveryOutcome.RECOVERED


def make_account(username: str = "", slot: int = 1) -> AccountConfig:
    return AccountConfig.model_validate(
        {
            "name": "test",
            "role": "farm",
            "ldplayer": {"index": 0, "adb_serial": "emulator-5554"},
            "login": {
                "server": "S9",
                "username": username,
                "password": "pw123" if username else "",
                "character_slot": slot,
            },
            "farm": {"map": "x", "gold_transfer_threshold": 1},
        }
    )


def make_ctx(
    tmp_path: Path,
    vision: ScriptedVision,
    device: FakeDevice,
    account: AccountConfig | None = None,
    max_recover: int = 1,
) -> TaskContext:
    settings: Settings = load_settings(SETTINGS_PATH)
    # trỏ coordinates về file thật trong repo; giảm quota recover cho test nhanh
    safety_cfg = settings.safety.model_copy(update={"max_recover_attempts": max_recover})
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
        account=account if account is not None else make_account(),
        log_dir=tmp_path / "logs",
    )


def test_happy_path_with_saved_credentials(tmp_path: Path) -> None:
    """App tắt -> title -> dialog -> form (creds đã lưu) -> chọn nv -> HUD."""
    vision = ScriptedVision(
        wait_any=[
            (login_mod.TPL_BTN_ENTER, match_at()),  # WAIT_TITLE
            (login_mod.TPL_SIGNIN_TITLE, match_at()),  # sau TAP_ENTER
            (login_mod.TPL_CHAR_SELECT, match_at()),  # sau SUBMIT
        ],
        wait_for=[
            match_at(410, 360),  # TAP_ENTER: nút Đăng nhập
            match_at(480, 172),  # SIGN_IN: nút 3CID
            match_at(480, 100),  # SIGN_IN: form hiện ra
            match_at(480, 358),  # SUBMIT: nút Sign In
            match_at(830, 477),  # SELECT_CHAR: nút Vào game
            match_at(820, 130),  # VERIFY: HUD
        ],
        find=[match_at(318, 285)],  # CHECK_SERVER: server_row khớp
    )
    device = FakeDevice(app_running=False)
    task = LoginTask(make_ctx(tmp_path, vision, device))

    result = task.run()

    assert result.status is TaskStatus.SUCCESS
    assert device.started == ["com.blackhole.jx2mobile"]
    # không gõ gì vì config không có credentials
    assert device.texts == [] and device.deletes == []
    # tap slot nhân vật 1 theo toạ độ fallback
    assert (828, 120) in device.taps


def test_credentials_are_typed_when_configured(tmp_path: Path) -> None:
    vision = ScriptedVision(
        wait_any=[
            (login_mod.TPL_FORM_TITLE, match_at()),  # WAIT_TITLE đáp thẳng vào form
            (login_mod.TPL_HUD, match_at()),  # sau SUBMIT vào thẳng thế giới
        ],
        wait_for=[
            match_at(480, 358),  # SUBMIT: nút Sign In
            match_at(820, 130),  # VERIFY: HUD
        ],
    )
    device = FakeDevice(app_running=True)
    account = make_account(username="farm01@mail.com")
    task = LoginTask(make_ctx(tmp_path, vision, device, account=account))

    result = task.run()

    assert result.status is TaskStatus.SUCCESS
    assert device.texts == ["farm01@mail.com", "pw123"]
    assert device.deletes == [40, 40]  # dọn cả 2 ô theo settings.login.clear_chars
    assert (480, 158) in device.taps and (480, 219) in device.taps


def test_character_creation_screen_aborts(tmp_path: Path) -> None:
    """Màn TẠO nhân vật = acc chưa có nhân vật -> ABORT, không bao giờ tạo."""
    vision = ScriptedVision(
        wait_any=[(login_mod.TPL_CHAR_CREATE, match_at())],  # WAIT_TITLE đáp vào màn tạo nv
    )
    device = FakeDevice(app_running=True)
    task = LoginTask(make_ctx(tmp_path, vision, device))

    result = task.run()

    assert result.status is TaskStatus.ABORTED
    assert device.taps == []  # tuyệt đối không bấm gì trên màn tạo nhân vật


def test_wrong_server_aborts(tmp_path: Path) -> None:
    vision = ScriptedVision(
        wait_any=[(login_mod.TPL_BTN_ENTER, match_at())],
        find=[None],  # server_row không khớp
    )
    device = FakeDevice(app_running=True)
    task = LoginTask(make_ctx(tmp_path, vision, device))

    result = task.run()

    assert result.status is TaskStatus.ABORTED


def test_missing_server_template_skips_check(tmp_path: Path) -> None:
    vision = ScriptedVision(
        wait_any=[
            (login_mod.TPL_BTN_ENTER, match_at()),
            (login_mod.TPL_HUD, match_at()),  # sau TAP_ENTER: đã trong thế giới
        ],
        wait_for=[
            match_at(410, 360),  # TAP_ENTER
            match_at(820, 130),  # VERIFY
        ],
        store_names=set(),  # store không có server_row
    )
    device = FakeDevice(app_running=True)
    task = LoginTask(make_ctx(tmp_path, vision, device))

    result = task.run()

    assert result.status is TaskStatus.SUCCESS


def test_signin_stuck_aborts_after_recover_budget(tmp_path: Path) -> None:
    """Sign In không chuyển màn -> recover -> resume ở WAIT_TITLE -> hết quota -> ABORT."""
    vision = ScriptedVision(
        wait_any=[
            (login_mod.TPL_FORM_TITLE, match_at()),
            None,  # SUBMIT: không chuyển màn
            None,  # WAIT_TITLE (resume sau recover): vẫn không thấy gì
        ],
        wait_for=[match_at()],  # nút Sign In cho lần bấm đầu
    )
    device = FakeDevice(app_running=True)
    task = LoginTask(make_ctx(tmp_path, vision, device, max_recover=1))

    result = task.run()

    assert result.status is TaskStatus.ABORTED
    # sau recovery, login resume ở WAIT_TITLE (màn hình có thể đã trôi đi nơi khác)
    assert result.final_state == "WAIT_TITLE"


def test_slot_out_of_range_aborts(tmp_path: Path) -> None:
    vision = ScriptedVision(
        wait_any=[(login_mod.TPL_CHAR_SELECT, match_at())],
    )
    device = FakeDevice(app_running=True)
    account = make_account(slot=9)
    task = LoginTask(make_ctx(tmp_path, vision, device, account=account))

    result = task.run()

    assert result.status is TaskStatus.ABORTED


def test_requires_account(tmp_path: Path) -> None:
    vision = ScriptedVision()
    device = FakeDevice()
    ctx = make_ctx(tmp_path, vision, device)
    ctx.account = None
    with pytest.raises(TaskDefinitionError, match=r"requires ctx\.account"):
        LoginTask(ctx)
