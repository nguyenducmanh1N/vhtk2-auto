"""Tests for YAML loading and pydantic validation in vhtk.config."""

from pathlib import Path

import pytest
import yaml

from vhtk.config import ConfigError, load_account, load_settings

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"
EXAMPLE_ACCOUNT_PATH = Path(__file__).parent.parent / "config" / "accounts" / "example.yaml"


def test_load_default_settings() -> None:
    settings = load_settings(SETTINGS_PATH)
    assert settings.safety.dry_run is True, "dry_run must default to ON"
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
    del data["safety"]["dry_run"]
    bad = tmp_path / "settings.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")

    with pytest.raises(ConfigError, match=r"safety\.dry_run"):
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
