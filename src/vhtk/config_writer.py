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
    deep_merge,
    local_settings_path,
)

_yaml = YAML()
_yaml.preserve_quotes = True
# giữ nguyên style thụt lề phổ biến của các file config hiện có
_yaml.indent(mapping=2, sequence=4, offset=2)


def _load_round_trip(path: Path, *, allow_empty: bool = False) -> CommentedMap:
    try:
        data = _yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # ruamel ném nhiều loại lỗi parse khác nhau
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None and allow_empty:
        return CommentedMap()
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
    """Update ONLY the ``safety`` section of the settings, keeping comments.

    The merged document is re-validated as a whole ``Settings`` before any
    file is touched, so a partial edit can never corrupt the config.

    Raises:
        ConfigError: if the merged document fails schema validation.
    """
    _write_settings_section(settings_path, ("safety",), safety.model_dump(mode="json"))


def write_settings_farm_skills(settings_path: Path, skills: FarmSkillSettings) -> None:
    """Update ONLY ``farm.skills`` of the settings, keeping comments.

    Raises:
        ConfigError: if the merged document fails schema validation.
    """
    _write_settings_section(settings_path, ("farm", "skills"), skills.model_dump(mode="json"))


def _write_settings_section(
    settings_path: Path, section: tuple[str, ...], values: dict[str, Any]
) -> None:
    """Write ``values`` under ``section``, each leaf to the file that owns it.

    A key already overridden in ``settings.local.yaml`` is updated THERE —
    writing it to settings.yaml would be silently shadowed by the override.
    Every other key goes to settings.yaml.
    """
    base = _load_round_trip(settings_path)
    local_path = local_settings_path(settings_path)
    local = _load_round_trip(local_path, allow_empty=True) if local_path.is_file() else None
    base_node = _ensure_map(base, section)
    local_node = _find_map(local, section)
    _apply_split(base_node, local_node, values)
    merged = deep_merge(_plain(base), _plain(local) if local is not None else {})
    try:
        Settings.model_validate(merged)
    except Exception as exc:
        raise ConfigError(f"merged settings would be invalid: {exc}") from exc
    _dump(base, settings_path)
    if local is not None and local_node is not None:
        _dump(local, local_path)


def _ensure_map(root: CommentedMap, section: tuple[str, ...]) -> CommentedMap:
    node = root
    for key in section:
        if not isinstance(node.get(key), CommentedMap):
            node[key] = CommentedMap()
        node = node[key]
    return node


def _find_map(root: CommentedMap | None, section: tuple[str, ...]) -> CommentedMap | None:
    node = root
    for key in section:
        if node is None:
            return None
        child = node.get(key)
        node = child if isinstance(child, CommentedMap) else None
    return node


def _apply_split(
    base: CommentedMap, local: CommentedMap | None, values: dict[str, Any]
) -> None:
    """Like :func:`_apply`, but leaves present in ``local`` are written there."""
    for key, value in values.items():
        local_child = local.get(key) if local is not None else None
        if isinstance(value, dict):
            if not isinstance(base.get(key), CommentedMap):
                base[key] = CommentedMap()
            _apply_split(
                base[key],
                local_child if isinstance(local_child, CommentedMap) else None,
                value,
            )
        elif local is not None and key in local:
            local[key] = value
        else:
            base[key] = value


def _plain(data: CommentedMap) -> dict[str, Any]:
    plain = _yaml.load(io.StringIO(_dump_to_str(data)))
    return plain if isinstance(plain, dict) else {}


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
