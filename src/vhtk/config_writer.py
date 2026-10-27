"""Write validated config back to YAML while PRESERVING comments.

The GUI (and any future tooling) must never write raw user input to disk:
every write goes model -> pydantic-validated object -> this module. Round-trip
editing uses ruamel.yaml so the Vietnamese comments in the config files — the
de-facto documentation of every knob — survive edits.

YAML files stay the single source of truth; this module only updates values.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from vhtk.config import (
    AccountConfig,
    ConfigError,
    Coordinates,
    FarmSkillSettings,
    SafetySettings,
    Settings,
)

_yaml = YAML()
_yaml.preserve_quotes = True
# giữ nguyên style thụt lề phổ biến của các file config hiện có
_yaml.indent(mapping=2, sequence=4, offset=2)


def _load_round_trip(path: Path) -> CommentedMap:
    try:
        data = _yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # ruamel ném nhiều loại lỗi parse khác nhau
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, CommentedMap):
        raise ConfigError(f"config file {path} must contain a YAML mapping at top level")
    return data


def _apply(target: CommentedMap, values: dict[str, Any]) -> None:
    """Recursively copy ``values`` into ``target`` without touching comments."""
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(target.get(key), CommentedMap):
            _apply(target[key], value)
        else:
            target[key] = value


def _dump(data: CommentedMap, path: Path) -> None:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")


def write_account(path: Path, account: AccountConfig, template: Path | None = None) -> None:
    """Write an account config to ``path``, preserving comments.

    An existing file is updated in place; a new file is seeded from
    ``template`` (normally ``example.yaml``) so new accounts inherit the
    documented layout. The caller passes an already-validated model.
    """
    if path.exists():
        base = _load_round_trip(path)
    elif template is not None and template.exists():
        base = _load_round_trip(template)
    else:
        base = CommentedMap()
    _apply(base, account.model_dump(mode="json"))
    _dump(base, path)


def write_settings_safety(settings_path: Path, safety: SafetySettings) -> None:
    """Update ONLY the ``safety`` section of settings.yaml, keeping comments.

    The merged document is re-validated as a whole ``Settings`` before the
    file is touched, so a partial edit can never corrupt the config.

    Raises:
        ConfigError: if the merged document fails schema validation.
    """
    base = _load_round_trip(settings_path)
    if "safety" not in base or not isinstance(base["safety"], CommentedMap):
        base["safety"] = CommentedMap()
    _apply(base["safety"], safety.model_dump(mode="json"))
    # validate toàn văn bản sau khi gộp — hỏng thì KHÔNG ghi file
    plain = _yaml.load(io.StringIO(_dump_to_str(base)))
    try:
        Settings.model_validate(plain)
    except Exception as exc:
        raise ConfigError(f"merged settings would be invalid: {exc}") from exc
    _dump(base, settings_path)


def write_settings_farm_skills(settings_path: Path, skills: FarmSkillSettings) -> None:
    """Update ONLY ``farm.skills`` of settings.yaml, keeping comments.

    Raises:
        ConfigError: if the merged document fails schema validation.
    """
    base = _load_round_trip(settings_path)
    if "farm" not in base or not isinstance(base["farm"], CommentedMap):
        base["farm"] = CommentedMap()
    if "skills" not in base["farm"] or not isinstance(base["farm"]["skills"], CommentedMap):
        base["farm"]["skills"] = CommentedMap()
    _apply(base["farm"]["skills"], skills.model_dump(mode="json"))
    plain = _yaml.load(io.StringIO(_dump_to_str(base)))
    try:
        Settings.model_validate(plain)
    except Exception as exc:
        raise ConfigError(f"merged settings would be invalid: {exc}") from exc
    _dump(base, settings_path)


def write_coordinates_skill_slots(
    coords_path: Path, skill_slots: list[tuple[int, int]]
) -> None:
    """Update ONLY ``farm.skill_slots`` of coordinates.yaml, keeping comments.

    Raises:
        ConfigError: if the merged document fails schema validation.
    """
    base = _load_round_trip(coords_path)
    if "farm" not in base or not isinstance(base["farm"], CommentedMap):
        base["farm"] = CommentedMap()
    base["farm"]["skill_slots"] = [list(p) for p in skill_slots]
    plain = _yaml.load(io.StringIO(_dump_to_str(base)))
    try:
        Coordinates.model_validate(plain)
    except Exception as exc:
        raise ConfigError(f"merged coordinates would be invalid: {exc}") from exc
    _dump(base, coords_path)


def _dump_to_str(data: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()
