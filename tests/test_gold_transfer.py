"""Tests for the W3 trade FSMs: barrier protocol, graph validity, and the
refuse-to-run guard when the trade UI contract is not captured yet."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import numpy as np

from vhtk.config import AccountConfig, Settings, load_settings
from vhtk.core.device import Device
from vhtk.core.fsm import TaskContext, TaskStatus
from vhtk.core.recovery import Recovery, RecoveryOutcome
from vhtk.core.safety import Safety
from vhtk.core.vision import Vision
from vhtk.notify import NullNotifier
from vhtk.tasks.gold_transfer import (
    Barrier,
    TransferFarmTask,
    TransferMainTask,
    barrier_file,
    other,
    phase_at_least,
)

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
REPO_ROOT = Path(__file__).parent.parent


class EmptyStore:
    def __init__(self) -> None:
        self.names: list[str] = []

    def __contains__(self, name: str) -> bool:
        return False


class BlindVision:
    store = EmptyStore()

    def capture_screen(self) -> np.ndarray:
        return np.zeros((540, 960, 3), dtype=np.uint8)

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


class OkRecovery:
    def check(self) -> RecoveryOutcome:
        return RecoveryOutcome.OK

    def on_state_failure(self, state: str, reason: str) -> RecoveryOutcome:
        return RecoveryOutcome.RECOVERED


def make_ctx(tmp_path: Path, role: str, stem: str) -> TaskContext:
    settings: Settings = load_settings(SETTINGS_PATH)
    settings = settings.model_copy(
        update={
            "paths": settings.paths.model_copy(
                update={"coordinates_file": REPO_ROOT / "config" / "coordinates.yaml"}
            ),
            "orchestrator": settings.orchestrator.model_copy(
                update={"workers_dir": tmp_path / "workers"}
            ),
            "transfer": settings.transfer.model_copy(
                update={"rendezvous_map": "Tuyền Châu", "rendezvous_spot": (700, 250)}
            ),
            "safety": settings.safety.model_copy(update={"max_recover_attempts": 1}),
        }
    )
    account = AccountConfig.model_validate(
        {
            "name": stem,
            "role": role,
            "ldplayer": {"index": 0, "adb_serial": "emulator-5554"},
            "login": {"server": "S9", "username": "", "password": ""},
            "farm": {"map": "Bắc Tuyền Châu", "gold_transfer_threshold": 1, "gold_keep": 10},
        }
    )
    notifier = NullNotifier()
    safety = Safety(
        settings.safety,
        notifier,
        tmp_path / "dry_run",
        kill_switch_path=tmp_path / "STOP",
        stem=stem,
    )
    return TaskContext(
        device=cast(Device, TapRecorder()),
        vision=cast(Vision, BlindVision()),
        safety=safety,
        recovery=cast(Recovery, OkRecovery()),
        notifier=notifier,
        settings=settings,
        account=account,
        log_dir=tmp_path / "logs",
    )


# -- barrier ---------------------------------------------------------------------


def test_phase_order_and_other_side() -> None:
    assert phase_at_least("locked", "arrived")
    assert phase_at_least("arrived", "arrived")
    assert not phase_at_least("arrived", "locked")
    assert not phase_at_least("failed", "arrived")  # failed không bao giờ "đạt"
    assert not phase_at_least("", "arrived")
    assert other("farm") == "main" and other("main") == "farm"


def test_barrier_roundtrip_and_side_updates(tmp_path: Path) -> None:
    b = Barrier(barrier_file(tmp_path / "workers"))
    assert b.read() == {}  # chưa có file -> rỗng, không nổ
    b.write({"session": "s1", "farm_stem": "farm01", "main_stem": "main"})
    b.update_side("farm", phase="arrived", money_before=1000)
    b.update_side("main", phase="arrived", money_before=5)
    b.update_side("farm", phase="offered", amount=990)
    assert b.phase("farm") == "offered" and b.phase("main") == "arrived"
    assert b.side_value("farm", "money_before") == 1000  # update không xoá khoá cũ
    assert b.side_value("farm", "amount") == 990
    assert b.read()["session"] == "s1"  # phần chung giữ nguyên


# -- FSM graphs -----------------------------------------------------------------


def test_both_graphs_validate_and_refuse_without_ui_contract(tmp_path: Path) -> None:
    """Chưa có template trade/ + toạ độ chưa đo -> VERIFY_HUD ABORT, không tap gì,
    hàng rào ghi failed kèm lý do để bên kia huỷ theo."""
    cases = ((TransferFarmTask, "farm", "farm01"), (TransferMainTask, "main", "main"))
    for cls, role, stem in cases:
        ctx = make_ctx(tmp_path, role, stem)
        Barrier(barrier_file(tmp_path / "workers")).write(
            {"session": "s", "farm_stem": "farm01", "main_stem": "main"}
        )
        task = cls(ctx)  # đồ thị state được validate ngay tại đây
        result = task.run()
        assert result.status is TaskStatus.ABORTED
        assert result.final_state == "VERIFY_HUD"
        assert cast(TapRecorder, ctx.device).taps == []
        b = Barrier(barrier_file(tmp_path / "workers"))
        assert b.phase(role) == "failed"  # type: ignore[arg-type]
        reason = str(b.side_value(role, "reason"))  # type: ignore[arg-type]
        assert "trade/window_header" in reason and "not measured" in reason


def test_refuses_when_barrier_names_another_account(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, "farm", "farm02")
    Barrier(barrier_file(tmp_path / "workers")).write(
        {"session": "s", "farm_stem": "farm01", "main_stem": "main"}
    )
    result = TransferFarmTask(ctx).run()
    assert result.status is TaskStatus.ABORTED
    reason = str(Barrier(barrier_file(tmp_path / "workers")).side_value("farm", "reason"))
    assert "does not name this account" in reason
