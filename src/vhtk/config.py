"""Load and validate YAML configuration with pydantic v2.

All runtime values (paths, thresholds, timeouts, blacklists) live in YAML;
code contains logic only. Validation errors are re-raised as ``ConfigError``
with a human-readable message pointing at the exact key and problem.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_SETTINGS_PATH = Path("config/settings.yaml")


class ConfigError(Exception):
    """Raised when a config file is missing, unreadable, or invalid."""


class _StrictModel(BaseModel):
    # Cấm key lạ để bắt lỗi gõ nhầm tên key ngay khi load
    model_config = ConfigDict(extra="forbid")


class Resolution(_StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    dpi: int = Field(gt=0)


class LDPlayerSettings(_StrictModel):
    ldconsole_path: str
    resolution: Resolution


class GameSettings(_StrictModel):
    package_name: str


class TemplateSettings(_StrictModel):
    dir: Path
    version: str
    match_threshold: float = Field(ge=0.0, le=1.0)

    @property
    def root(self) -> Path:
        """Root directory of the active template version."""
        return self.dir / self.version


class VisionSettings(_StrictModel):
    default_timeout: float = Field(gt=0)
    poll_interval: float = Field(gt=0)
    grayscale_match: bool
    nms_overlap_threshold: float = Field(ge=0.0, le=1.0)


class OcrSettings(_StrictModel):
    lang: str


class ActionDelay(_StrictModel):
    min_seconds: float = Field(ge=0)
    max_seconds: float = Field(ge=0)


class SafetySettings(_StrictModel):
    dry_run: bool
    confirm_mode: bool
    confirm_timeout_seconds: float = Field(gt=0)
    kill_switch_file: str
    max_actions_per_minute: int = Field(gt=0)
    max_irreversible_per_session: int = Field(gt=0)
    max_recover_attempts: int = Field(gt=0)
    action_delay: ActionDelay


class SellSettings(_StrictModel):
    quality_blacklist_threshold: int = Field(ge=0)
    item_blacklist: list[str]


class HSVRange(_StrictModel):
    """One HSV color range, OpenCV convention: H in 0..179, S/V in 0..255."""

    h: tuple[int, int]
    s: tuple[int, int]
    v: tuple[int, int]


class QualitySettings(_StrictModel):
    """Item-quality detection via border color of an item cell."""

    border_width: int = Field(gt=0)
    min_match_fraction: float = Field(ge=0.0, le=1.0)
    colors: dict[str, HSVRange]


class TelegramSettings(_StrictModel):
    enabled: bool
    bot_token: str = ""
    chat_id: str = ""


class LoggingSettings(_StrictModel):
    dir: Path
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class PathSettings(_StrictModel):
    accounts_dir: Path
    coordinates_file: Path


class Settings(_StrictModel):
    """Top-level schema of ``config/settings.yaml``."""

    ldplayer: LDPlayerSettings
    game: GameSettings
    templates: TemplateSettings
    vision: VisionSettings
    ocr: OcrSettings
    safety: SafetySettings
    sell: SellSettings
    quality: QualitySettings
    telegram: TelegramSettings
    logging: LoggingSettings
    paths: PathSettings


class AccountLDPlayer(_StrictModel):
    index: int = Field(ge=0)
    adb_serial: str


class AccountLogin(_StrictModel):
    server: str
    username: str
    password: str


class AccountFarm(_StrictModel):
    map: str
    gold_transfer_threshold: int = Field(ge=0)


class AccountConfig(_StrictModel):
    """Schema of one file in ``config/accounts/``."""

    name: str
    role: Literal["main", "farm"]
    ldplayer: AccountLDPlayer
    login: AccountLogin
    farm: AccountFarm


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a YAML mapping at top level")
    return data


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"invalid config in {path}:"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_settings(path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """Load and validate the main settings file.

    Raises:
        ConfigError: if the file is missing, is not valid YAML, or fails
            schema validation (missing key, wrong type, unknown key).
    """
    data = _read_yaml(path)
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_account(path: Path) -> AccountConfig:
    """Load and validate a single account config file.

    Raises:
        ConfigError: same conditions as :func:`load_settings`.
    """
    data = _read_yaml(path)
    try:
        return AccountConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(path, exc)) from exc


def load_all_accounts(accounts_dir: Path) -> list[AccountConfig]:
    """Load every account YAML in ``accounts_dir``, skipping ``example.yaml``."""
    if not accounts_dir.is_dir():
        raise ConfigError(f"accounts directory not found: {accounts_dir}")
    accounts = [
        load_account(f) for f in sorted(accounts_dir.glob("*.yaml")) if f.name != "example.yaml"
    ]
    if not accounts:
        raise ConfigError(
            f"no account config found in {accounts_dir} "
            "(copy example.yaml to e.g. main.yaml and edit it)"
        )
    return accounts
