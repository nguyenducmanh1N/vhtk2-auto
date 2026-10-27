"""Tests for YAML loading and pydantic validation in vhtk.config."""

from pathlib import Path

import pytest
import yaml

from vhtk.config import ConfigError, load_account, load_settings

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
EXAMPLE_ACCOUNT_PATH = Path(__file__).parent.parent / "config" / "accounts" / "example.yaml"


def test_load_default_settings() -> None:
    settings = load_settings(SETTINGS_PATH)
    # chốt an toàn nằm ở MẶC ĐỊNH SCHEMA (file thật có thể tắt tạm khi test
    # có giám sát): YAML thiếu khoá -> dry_run/confirm_mode phải BẬT
    from vhtk.config import SafetySettings

    assert SafetySettings.model_fields["dry_run"].default is True, "dry_run must default to ON"
    assert SafetySettings.model_fields["confirm_mode"].default is True
    assert settings.ldplayer.resolution.width == 960
    assert settings.templates.root == settings.templates.dir / settings.templates.version


def test_load_example_account() -> None:
    account = load_account(EXAMPLE_ACCOUNT_PATH)
    assert account.role in ("main", "farm")


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")


def test_missing_key_reports_location(tmp_path: Path) -> None:
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    # dry_run giờ có mặc định an toàn (True) nên thiếu không còn là lỗi —
    # dùng khoá bắt buộc khác để kiểm thông điệp vị trí lỗi
    del data["safety"]["kill_switch_file"]
    bad = tmp_path / "settings.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigError, match=r"safety\.kill_switch_file"):
        load_settings(bad)


def test_wrong_type_reports_location(tmp_path: Path) -> None:
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    data["templates"]["match_threshold"] = "high"
    bad = tmp_path / "settings.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigError, match=r"templates\.match_threshold"):
        load_settings(bad)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8"))
    data["safety"]["dryrun_typo"] = True
    bad = tmp_path / "settings.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigError, match="dryrun_typo"):
        load_settings(bad)
