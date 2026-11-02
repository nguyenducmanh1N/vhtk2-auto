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


def _copy_settings(tmp_path: Path) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(SETTINGS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_local_overrides_merged_per_key(tmp_path: Path) -> None:
    path = _copy_settings(tmp_path)
    base = load_settings(path)
    (tmp_path / "settings.local.yaml").write_text(
        "templates:\n  version: v9-local\norchestrator:\n  manage_instances: false\n",
        encoding="utf-8",
    )
    merged = load_settings(path)
    assert merged.templates.version == "v9-local"
    assert merged.orchestrator.manage_instances is False
    # khoá anh em không ghi trong file local giữ nguyên giá trị của settings.yaml
    assert merged.templates.match_threshold == base.templates.match_threshold
    assert merged.orchestrator.boot_timeout == base.orchestrator.boot_timeout


def test_empty_local_file_is_ignored(tmp_path: Path) -> None:
    path = _copy_settings(tmp_path)
    expected = load_settings(path)
    (tmp_path / "settings.local.yaml").write_text("# chỉ có comment\n", encoding="utf-8")
    assert load_settings(path) == expected


def test_local_typo_rejected_naming_local_file(tmp_path: Path) -> None:
    path = _copy_settings(tmp_path)
    (tmp_path / "settings.local.yaml").write_text(
        "safety:\n  dryrun_typo: true\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=r"settings\.local\.yaml[\s\S]*dryrun_typo"):
        load_settings(path)


def test_local_file_must_be_mapping(tmp_path: Path) -> None:
    path = _copy_settings(tmp_path)
    (tmp_path / "settings.local.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_settings(path)


def test_deep_merge_replaces_lists() -> None:
    from vhtk.config import deep_merge

    base = {"a": {"b": 1, "c": [1, 2, 3]}, "d": 4}
    assert deep_merge(base, {"a": {"c": [9]}}) == {"a": {"b": 1, "c": [9]}, "d": 4}
    assert base == {"a": {"b": 1, "c": [1, 2, 3]}, "d": 4}  # không sửa tại chỗ
