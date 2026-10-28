"""Tests for the irreversible-action gate: dry_run, confirm_mode, limits."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vhtk.config import ActionDelay, SafetySettings
from vhtk.core.errors import KillSwitchTriggered, SafetyLimitExceeded
from vhtk.core.safety import Image, Safety


def make_cfg(
    *,
    dry_run: bool = False,
    confirm_mode: bool = False,
    max_irreversible: int = 5,
) -> SafetySettings:
    return SafetySettings(
        dry_run=dry_run,
        confirm_mode=confirm_mode,
        confirm_timeout_seconds=60.0,
        kill_switch_file="STOP",
        max_actions_per_minute=30,
        max_irreversible_per_session=max_irreversible,
        max_recover_attempts=3,
        action_delay=ActionDelay(min_seconds=0.0, max_seconds=0.0),
    )


class StubNotifier:
    """Records every call; answers confirms with a scripted value."""

    def __init__(self, confirm_answer: bool = False) -> None:
        self.confirm_answer = confirm_answer
        self.texts: list[str] = []
        self.photos: list[str] = []
        self.confirm_prompts: list[str] = []

    def send_text(self, text: str) -> None:
        self.texts.append(text)

    def send_photo(self, image: Image, caption: str) -> None:
        self.photos.append(caption)

    def wait_confirm(self, prompt: str, image: Image | None, timeout: float) -> bool:
        self.confirm_prompts.append(prompt)
        return self.confirm_answer


def frame() -> Image:
    return np.zeros((10, 10, 3), dtype=np.uint8)


def make_safety(
    tmp_path: Path, cfg: SafetySettings, notifier: StubNotifier | None = None
) -> tuple[Safety, StubNotifier]:
    n = notifier or StubNotifier()
    safety = Safety(
        cfg, n, evidence_dir=tmp_path / "dry_run", kill_switch_path=tmp_path / "STOP"
    )
    return safety, n


def test_dry_run_skips_and_saves_evidence(tmp_path: Path) -> None:
    safety, notifier = make_safety(tmp_path, make_cfg(dry_run=True, confirm_mode=True))

    allowed = safety.guard_irreversible("sell_item", "sell junk", screenshot=frame())

    assert allowed is False
    assert safety.executed_irreversible == 0
    saved = list((tmp_path / "dry_run").glob("*.png"))
    assert len(saved) == 1 and "sell_item" in saved[0].name
    # dry_run có ưu tiên cao hơn confirm_mode — không được hỏi người
    assert notifier.confirm_prompts == []


def test_dry_run_without_screenshot_still_skips(tmp_path: Path) -> None:
    safety, _ = make_safety(tmp_path, make_cfg(dry_run=True))

    assert safety.guard_irreversible("buy_item", "buy weapon") is False
    assert not (tmp_path / "dry_run").exists()


def test_confirm_approved_executes_and_counts(tmp_path: Path) -> None:
    safety, notifier = make_safety(
        tmp_path, make_cfg(confirm_mode=True), StubNotifier(confirm_answer=True)
    )

    assert safety.guard_irreversible("trade_gold", "send 100k gold", frame()) is True
    assert safety.executed_irreversible == 1
    assert len(notifier.confirm_prompts) == 1
    assert "trade_gold" in notifier.confirm_prompts[0]


def test_confirm_denied_or_timeout_blocks(tmp_path: Path) -> None:
    safety, notifier = make_safety(
        tmp_path, make_cfg(confirm_mode=True), StubNotifier(confirm_answer=False)
    )

    assert safety.guard_irreversible("sell_item", "sell junk") is False
    assert safety.executed_irreversible == 0
    assert len(notifier.confirm_prompts) == 1


def test_no_guards_executes_immediately(tmp_path: Path) -> None:
    safety, notifier = make_safety(tmp_path, make_cfg())

    assert safety.guard_irreversible("sell_item", "sell junk") is True
    assert safety.executed_irreversible == 1
    assert notifier.confirm_prompts == []


def test_session_limit_raises(tmp_path: Path) -> None:
    safety, _ = make_safety(tmp_path, make_cfg(max_irreversible=2))

    assert safety.guard_irreversible("sell_item", "1") is True
    assert safety.guard_irreversible("sell_item", "2") is True
    with pytest.raises(SafetyLimitExceeded, match="limit of 2"):
        safety.guard_irreversible("sell_item", "3")


def test_denied_actions_do_not_consume_limit(tmp_path: Path) -> None:
    safety, _ = make_safety(tmp_path, make_cfg(dry_run=True, max_irreversible=1))

    # dry_run bỏ qua hành động — không được tính vào quota thực thi
    for _ in range(5):
        assert safety.guard_irreversible("sell_item", "x") is False
    assert safety.executed_irreversible == 0


def test_kill_switch_blocks_guard(tmp_path: Path) -> None:
    safety, _ = make_safety(tmp_path, make_cfg())
    (tmp_path / "STOP").touch()

    with pytest.raises(KillSwitchTriggered):
        safety.guard_irreversible("sell_item", "x")
    with pytest.raises(KillSwitchTriggered):
        safety.check_kill_switch()


def test_per_account_kill_switch(tmp_path: Path) -> None:
    """STOP.<stem> dừng đúng worker đó; STOP toàn cục dừng mọi worker."""
    cfg = make_cfg()
    n = StubNotifier()
    safety = Safety(
        cfg, n, evidence_dir=tmp_path / "d", kill_switch_path=tmp_path / "STOP", stem="farm01"
    )
    other = Safety(
        cfg, n, evidence_dir=tmp_path / "d", kill_switch_path=tmp_path / "STOP", stem="farm02"
    )
    safety.check_kill_switch()  # chưa có file -> không nổ
    (tmp_path / "STOP.farm01").touch()
    with pytest.raises(KillSwitchTriggered):
        safety.check_kill_switch()
    other.check_kill_switch()  # worker khác không bị ảnh hưởng
    (tmp_path / "STOP.farm01").unlink()
    (tmp_path / "STOP").touch()  # file toàn cục chặn tất cả
    with pytest.raises(KillSwitchTriggered):
        other.check_kill_switch()
