"""Tests for the management app: config_writer round-trips and the REST API."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml as pyyaml
from fastapi.testclient import TestClient

from vhtk.config import ConfigError, load_account, load_settings
from vhtk.config_writer import write_account, write_settings_safety
from vhtk.ui.server import create_app

REPO = Path(__file__).parent.parent


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Copy the real config tree into a tmp dir and point settings at it."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy(REPO / "config" / "settings.yaml", cfg / "settings.yaml")
    shutil.copy(REPO / "config" / "coordinates.yaml", cfg / "coordinates.yaml")
    (cfg / "accounts").mkdir()
    shutil.copy(REPO / "config" / "accounts" / "example.yaml", cfg / "accounts" / "example.yaml")
    # settings trỏ accounts_dir/coordinates về tmp (đường dẫn tuyệt đối)
    text = (cfg / "settings.yaml").read_text(encoding="utf-8")
    text = text.replace("accounts_dir: config/accounts", f"accounts_dir: {cfg / 'accounts'}")
    text = text.replace(
        "coordinates_file: config/coordinates.yaml", f"coordinates_file: {cfg / 'coordinates.yaml'}"
    )
    (cfg / "settings.yaml").write_text(text, encoding="utf-8")
    return cfg


@pytest.fixture()
def client(workspace: Path) -> TestClient:
    return TestClient(create_app(workspace / "settings.yaml"))


def sample_account(name: str = "farm01") -> dict[str, object]:
    return {
        "name": name,
        "role": "farm",
        "ldplayer": {"index": 1, "adb_serial": "emulator-5556"},
        "login": {
            "server": "S9.Hàng Long(Mới)",
            "username": "user01",
            "password": "pw",
            "character_slot": 2,
        },
        "farm": {"map": "Tuyền Châu", "gold_transfer_threshold": 500000},
    }


# ---------------------------------------------------------------------------
# config_writer
# ---------------------------------------------------------------------------


def test_write_account_preserves_comments(workspace: Path) -> None:
    from vhtk.config import AccountConfig

    target = workspace / "accounts" / "farm01.yaml"
    account = AccountConfig.model_validate(sample_account())
    write_account(target, account, template=workspace / "accounts" / "example.yaml")

    text = target.read_text(encoding="utf-8")
    # comment tiếng Việt của example.yaml phải sống sót
    assert "Slot nhân vật" in text
    # và giá trị là của account mới
    loaded = load_account(target)
    assert loaded.login.character_slot == 2
    assert loaded.farm.map == "Tuyền Châu"

    # sửa lần 2: comment vẫn còn, giá trị cập nhật
    account2 = account.model_copy(deep=True)
    account2.farm.gold_transfer_threshold = 999
    write_account(target, account2)
    text2 = target.read_text(encoding="utf-8")
    assert "Slot nhân vật" in text2
    assert load_account(target).farm.gold_transfer_threshold == 999


def test_write_settings_safety_preserves_rest(workspace: Path) -> None:
    settings_path = workspace / "settings.yaml"
    before = load_settings(settings_path)
    safety = before.safety.model_copy(update={"dry_run": False, "max_actions_per_minute": 99})

    write_settings_safety(settings_path, safety)

    after = load_settings(settings_path)
    assert after.safety.dry_run is False
    assert after.safety.max_actions_per_minute == 99
    # phần khác không bị đụng
    assert after.game.package_name == before.game.package_name
    assert "Chế độ khô" in settings_path.read_text(encoding="utf-8")  # comment còn


def test_write_settings_rejects_invalid_merge(workspace: Path) -> None:
    settings_path = workspace / "settings.yaml"
    # phá file: xoá key bắt buộc ngoài safety rồi thử ghi -> phải từ chối
    data = pyyaml.safe_load(settings_path.read_text(encoding="utf-8"))
    del data["game"]
    settings_path.write_text(pyyaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    from vhtk.config import SafetySettings

    good_safety = SafetySettings.model_validate(
        {
            "dry_run": True,
            "confirm_mode": True,
            "confirm_timeout_seconds": 60,
            "kill_switch_file": "STOP",
            "max_actions_per_minute": 30,
            "max_irreversible_per_session": 10,
            "max_recover_attempts": 3,
            "action_delay": {"min_seconds": 0, "max_seconds": 1},
        }
    )
    with pytest.raises(ConfigError, match="invalid"):
        write_settings_safety(settings_path, good_safety)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


def test_account_crud_cycle(client: TestClient, workspace: Path) -> None:
    # tạo
    r = client.post("/api/accounts/farm01", json=sample_account())
    assert r.status_code == 200, r.text
    assert (workspace / "accounts" / "farm01.yaml").is_file()
    # trùng tên -> 409
    assert client.post("/api/accounts/farm01", json=sample_account()).status_code == 409
    # danh sách
    rows = client.get("/api/accounts").json()
    assert [row["stem"] for row in rows] == ["farm01"]
    assert rows[0]["role"] == "farm"
    # đọc
    acc = client.get("/api/accounts/farm01").json()
    assert acc["login"]["username"] == "user01"
    # sửa
    acc["farm"]["map"] = "Giang Tân"
    acc["role"] = "main"
    assert client.put("/api/accounts/farm01", json=acc).status_code == 200
    assert load_account(workspace / "accounts" / "farm01.yaml").role == "main"
    # xoá
    assert client.delete("/api/accounts/farm01").status_code == 200
    assert client.get("/api/accounts").json() == []


def test_invalid_account_rejected_with_field_errors(client: TestClient) -> None:
    bad = sample_account()
    bad["role"] = "boss"  # không nằm trong Literal
    r = client.post("/api/accounts/farm02", json=bad)
    assert r.status_code == 422
    fields = [e["field"] for e in r.json()["detail"]]
    assert "role" in fields


def test_stem_validation_blocks_traversal(client: TestClient) -> None:
    assert client.get("/api/accounts/..%2Fsettings").status_code in (400, 404)
    assert client.put("/api/accounts/example", json=sample_account()).status_code == 400


def test_safety_get_and_put(client: TestClient, workspace: Path) -> None:
    s = client.get("/api/settings/safety").json()
    # không chốt giá trị dry_run ban đầu — file settings.yaml thật do người
    # vận hành bật/tắt (27/10 đang tắt để test bán thật trên AVD)
    assert isinstance(s["dry_run"], bool)
    s["confirm_timeout_seconds"] = 120.0
    assert client.put("/api/settings/safety", json=s).status_code == 200
    assert load_settings(workspace / "settings.yaml").safety.confirm_timeout_seconds == 120.0


def test_safety_put_invalid_rejected(client: TestClient) -> None:
    s = client.get("/api/settings/safety").json()
    s["max_actions_per_minute"] = 0  # phải > 0
    r = client.put("/api/settings/safety", json=s)
    assert r.status_code == 422


def test_farm_skills_get_and_put(client: TestClient, workspace: Path) -> None:
    s = client.get("/api/settings/farm-skills").json()
    # không chốt giá trị enabled ban đầu — file settings.yaml thật do người
    # vận hành bật/tắt qua GUI, test chỉ cần roundtrip đúng
    assert isinstance(s["enabled"], bool)
    assert len(s["slot_points"]) == 8
    s.update(enabled=True, slots=[1, 2, 3], heal_slot=8, heal_hp_percent=55)
    s["slot_points"][0] = [777, 444]
    assert client.put("/api/settings/farm-skills", json=s).status_code == 200, "save failed"
    after = load_settings(workspace / "settings.yaml")
    assert after.farm.skills.enabled is True
    assert after.farm.skills.slots == [1, 2, 3]
    assert after.farm.skills.heal_slot == 8
    from vhtk.config import load_coordinates

    coords = load_coordinates(workspace / "coordinates.yaml")
    assert coords.farm.skill_slots[0] == (777, 444)
    # comment của 2 file còn sống sau khi ghi
    assert "Auto nhấn chiêu" in (workspace / "settings.yaml").read_text(encoding="utf-8")
    assert "vòng chiêu" in (workspace / "coordinates.yaml").read_text(encoding="utf-8")


def test_farm_skills_put_rejects_heal_slot_in_rotation(client: TestClient) -> None:
    s = client.get("/api/settings/farm-skills").json()
    s.update(slots=[1, 2], heal_slot=2)
    assert client.put("/api/settings/farm-skills", json=s).status_code == 422


def test_farm_skills_put_rejects_bad_slot_points(client: TestClient) -> None:
    s = client.get("/api/settings/farm-skills").json()
    s["slot_points"] = [[1, 2]] * 7  # phải đúng 8 ô
    assert client.put("/api/settings/farm-skills", json=s).status_code == 422


def test_broken_account_file_listed_with_error(client: TestClient, workspace: Path) -> None:
    (workspace / "accounts" / "hong.yaml").write_text("name: [oops", encoding="utf-8")
    rows = client.get("/api/accounts").json()
    assert any(row.get("error") for row in rows if row["stem"] == "hong")


def test_farm_maps_lists_ready_template_pairs(client: TestClient) -> None:
    """Chỉ map có đủ cặp row_<slug> + hud_name_<slug> mới được liệt kê,
    tên hiển thị lấy từ display_name trong manifest."""
    maps = client.get("/api/farm-maps").json()
    by_slug = {m["slug"]: m["name"] for m in maps}
    # 3 map đã chụp đủ cặp trên AVD (templates/v1.0-avd — settings test trỏ repo thật)
    assert by_slug.get("tuyen_chau") == "Tuyền Châu"
    assert by_slug.get("long_tuyen_thon") == "Long Tuyền Thôn"
    assert by_slug.get("bac_tuyen_chau") == "Bắc Tuyền Châu"


def test_runner_restart_unknown_account_404(client: TestClient) -> None:
    r = client.post("/api/runner/restart", json={"account": "khongcoai"})
    assert r.status_code == 404


def test_runner_status_shape(client: TestClient) -> None:
    s = client.get("/api/runner").json()
    assert "running" in s and "pids" in s


def test_index_served(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "VHTK2" in r.text
