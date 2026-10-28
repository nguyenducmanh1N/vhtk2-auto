"""FastAPI backend of the management app (phase D1).

Thin shell over the YAML configs: every read goes through the same pydantic
loaders the tool itself uses, every write through ``config_writer`` (validated
first, comments preserved). No game/business logic lives here.

The server binds to localhost only; it is started by ``python -m vhtk.ui``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from vhtk.config import (
    AccountConfig,
    ConfigError,
    FarmSkillSettings,
    SafetySettings,
    load_account,
    load_coordinates,
    load_settings,
)
from vhtk.config_writer import (
    write_account,
    write_coordinates_skill_slots,
    write_settings_farm_skills,
    write_settings_safety,
)

log = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
EXAMPLE_NAME = "example.yaml"
# Tên file acc: chữ/số/gạch — chặn path traversal từ UI
_STEM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validation_detail(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
        for err in exc.errors()
    ]


def create_app(settings_path: Path = Path("config/settings.yaml")) -> FastAPI:
    """Build the FastAPI app bound to one settings file."""
    app = FastAPI(title="VHTK2 Auto Tool — Quản lý", docs_url=None, redoc_url=None)

    @app.exception_handler(ConfigError)
    def config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
        # settings.yaml/schema lệch nhau (vd server chạy code cũ) -> báo rõ
        # thay vì 500 trống trơn
        log.error("config error while serving", detail=str(exc))
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    def accounts_dir() -> Path:
        # đọc lại mỗi lần để tôn trọng thay đổi settings giữa chừng
        return load_settings(settings_path).paths.accounts_dir

    def account_path(stem: str) -> Path:
        if not _STEM_RE.fullmatch(stem) or stem == "example":
            raise HTTPException(400, detail=f"ten file khong hop le: {stem!r}")
        return accounts_dir() / f"{stem}.yaml"

    # -- accounts --------------------------------------------------------------

    @app.get("/api/accounts")
    def list_accounts() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        folder = accounts_dir()
        if not folder.is_dir():
            return rows
        for f in sorted(folder.glob("*.yaml")):
            if f.name == EXAMPLE_NAME:
                continue
            try:
                acc = load_account(f)
                rows.append(
                    {
                        "stem": f.stem,
                        "name": acc.name,
                        "role": acc.role,
                        "server": acc.login.server,
                        "adb_serial": acc.ldplayer.adb_serial,
                        "ldplayer_index": acc.ldplayer.index,
                    }
                )
            except ConfigError as exc:
                rows.append({"stem": f.stem, "error": str(exc)})
        return rows

    @app.get("/api/accounts/{stem}")
    def get_account(stem: str) -> dict[str, Any]:
        path = account_path(stem)
        if not path.is_file():
            raise HTTPException(404, detail=f"khong tim thay tai khoan {stem!r}")
        try:
            return {"stem": stem, **load_account(path).model_dump(mode="json")}
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @app.get("/api/accounts/{stem}/screenshot")
    def account_screenshot(stem: str) -> Response:
        """One raw screencap of the device bound to this account.

        Phục vụ công cụ "chọn điểm tap trên ảnh bản đồ" của GUI: người dùng
        tự mở màn bản đồ trong game rồi click lên ảnh chụp. Chỉ là một lệnh
        adb screencap — không có logic game, không tap gì cả.
        """
        path = account_path(stem)
        if not path.is_file():
            raise HTTPException(404, detail=f"khong tim thay tai khoan {stem!r}")
        serial = load_account(path).ldplayer.adb_serial
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(502, detail=f"adb loi: {exc}") from exc
        if r.returncode != 0 or not r.stdout.startswith(b"\x89PNG"):
            err = r.stderr.decode(errors="replace").strip() or "khong co du lieu PNG"
            raise HTTPException(502, detail=f"khong chup duoc man hinh {serial}: {err}")
        return Response(content=r.stdout, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.put("/api/accounts/{stem}")
    def put_account(stem: str, body: dict[str, Any]) -> dict[str, str]:
        path = account_path(stem)
        body.pop("stem", None)
        try:
            account = AccountConfig.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, detail=_validation_detail(exc)) from exc
        write_account(path, account, template=accounts_dir() / EXAMPLE_NAME)
        log.info("account saved", stem=stem)
        return {"status": "saved", "stem": stem}

    @app.post("/api/accounts/{stem}")
    def create_account(stem: str, body: dict[str, Any]) -> dict[str, str]:
        path = account_path(stem)
        if path.exists():
            raise HTTPException(409, detail=f"tai khoan {stem!r} da ton tai")
        return put_account(stem, body)

    @app.delete("/api/accounts/{stem}")
    def delete_account(stem: str) -> dict[str, str]:
        path = account_path(stem)
        if not path.is_file():
            raise HTTPException(404, detail=f"khong tim thay tai khoan {stem!r}")
        path.unlink()
        log.warning("account deleted", stem=stem)
        return {"status": "deleted", "stem": stem}

    @app.get("/api/accounts-template")
    def account_template() -> dict[str, Any]:
        """Default values for the "new account" form, taken from example.yaml."""
        example = accounts_dir() / EXAMPLE_NAME
        if example.is_file():
            try:
                return load_account(example).model_dump(mode="json")
            except ConfigError:
                pass
        raise HTTPException(404, detail="example.yaml khong doc duoc")

    # -- farm maps (các map có đủ template để chạy) -------------------------------

    @app.get("/api/farm-maps")
    def farm_maps() -> list[dict[str, str]]:
        """Maps ready to farm: template pair ``row_<slug>`` + ``hud_name_<slug>``
        both exist in the active template version. Display names come from
        ``display_name`` in the template manifest."""
        settings = load_settings(settings_path)
        map_dir = settings.templates.root / "map"
        manifest: dict[str, Any] = {}
        mf = settings.templates.root / "manifest.yaml"
        if mf.is_file():
            raw = yaml.safe_load(mf.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                manifest = raw
        out: list[dict[str, str]] = []
        if not map_dir.is_dir():
            return out
        for row in sorted(map_dir.glob("row_*.png")):
            slug = row.stem.removeprefix("row_")
            if not (map_dir / f"hud_name_{slug}.png").is_file():
                continue  # thiếu nửa cặp -> map chưa sẵn sàng, không cho chọn
            meta = manifest.get(f"map/row_{slug}")
            name = meta.get("display_name") if isinstance(meta, dict) else None
            out.append({"slug": slug, "name": str(name) if name else slug.replace("_", " ")})
        return out

    # -- map library (kho ảnh map đã quét bằng tools/harvest_maps.py) --------------

    def maps_dir() -> Path:
        return Path("data/maps") / load_settings(settings_path).templates.version

    def maps_manifest() -> dict[str, Any]:
        mf = maps_dir() / "manifest.yaml"
        if not mf.is_file():
            return {"maps": [], "map_image_rect": None}
        raw = yaml.safe_load(mf.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"maps": [], "map_image_rect": None}

    @app.get("/api/maps")
    def map_library() -> dict[str, Any]:
        """Harvested map previews + per-map pixel<->game-coord calibration."""
        data = maps_manifest()
        return {
            "map_image_rect": data.get("map_image_rect"),
            "maps": [
                {
                    "index": e["index"],
                    "name": e["name"],
                    "calibration": e.get("calibration"),
                }
                for e in data.get("maps", [])
                if e.get("name")
            ],
        }

    @app.get("/api/maps/{index}/image")
    def map_image(index: int) -> FileResponse:
        for e in maps_manifest().get("maps", []):
            if e["index"] == index:
                f = maps_dir() / e["files"]["map"]
                if f.is_file():
                    return FileResponse(f, media_type="image/png")
        raise HTTPException(404, detail=f"khong co anh map index {index}")

    # -- runner control (giải pháp tạm cho D1 — W2 chuyển sang API orchestrator) --

    def runner_pids() -> list[int]:
        r = subprocess.run(
            ["pgrep", "-f", "tools/run_farm.py"], capture_output=True, text=True, check=False
        )
        return [int(x) for x in r.stdout.split()]

    @app.get("/api/runner")
    def runner_status() -> dict[str, Any]:
        pids = runner_pids()
        return {"running": bool(pids), "pids": pids}

    @app.post("/api/runner/restart")
    def runner_restart(body: dict[str, Any]) -> dict[str, Any]:
        """Stop the farm runner cleanly (kill-switch) and start it again with
        the given account — so a saved config actually takes effect.

        Process control only; no game logic lives here. The stop goes through
        the same STOP-file mechanism a human would use.
        """
        stem = str(body.get("account", ""))
        path = account_path(stem)
        if not path.is_file():
            raise HTTPException(404, detail=f"khong tim thay tai khoan {stem!r}")
        settings = load_settings(settings_path)
        stop_file = Path(settings.safety.kill_switch_file)
        if runner_pids():
            stop_file.touch()
            deadline = time.monotonic() + 60.0
            while runner_pids() and time.monotonic() < deadline:
                time.sleep(0.5)
            if runner_pids():
                log.warning("runner did not stop via kill-switch, killing")
                subprocess.run(["pkill", "-f", "tools/run_farm.py"], check=False)
                time.sleep(1.0)
        stop_file.unlink(missing_ok=True)
        log_dir = settings.logging.dir
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "runner.out").open("ab") as out:
            proc = subprocess.Popen(
                [sys.executable, "tools/run_farm.py", "--account", str(path)],
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        log.info("runner restarted from GUI", account=stem, pid=proc.pid)
        return {"status": "restarted", "pid": proc.pid, "account": stem}

    # -- settings (safety only, per D1 scope) -----------------------------------

    @app.get("/api/settings/safety")
    def get_safety() -> dict[str, Any]:
        return load_settings(settings_path).safety.model_dump(mode="json")

    @app.put("/api/settings/safety")
    def put_safety(body: dict[str, Any]) -> dict[str, str]:
        try:
            safety = SafetySettings.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, detail=_validation_detail(exc)) from exc
        try:
            write_settings_safety(settings_path, safety)
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        log.info(
            "safety settings saved",
            dry_run=safety.dry_run,
            confirm_mode=safety.confirm_mode,
        )
        return {"status": "saved"}

    # -- farm skills (auto nhấn chiêu + vật phẩm hồi máu) --------------------------

    @app.get("/api/settings/farm-skills")
    def get_farm_skills() -> dict[str, Any]:
        """``farm.skills`` of settings.yaml + the 8 slot points of coordinates.yaml."""
        settings = load_settings(settings_path)
        coords = load_coordinates(settings.paths.coordinates_file)
        points = [list(p) for p in coords.farm.skill_slots] or [[0, 0]] * 8
        return {**settings.farm.skills.model_dump(mode="json"), "slot_points": points}

    @app.put("/api/settings/farm-skills")
    def put_farm_skills(body: dict[str, Any]) -> dict[str, str]:
        raw_points = body.pop("slot_points", None)
        try:
            skills = FarmSkillSettings.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, detail=_validation_detail(exc)) from exc
        points: list[tuple[int, int]] | None = None
        if raw_points is not None:
            if (
                not isinstance(raw_points, list)
                or len(raw_points) != 8
                or not all(
                    isinstance(p, list) and len(p) == 2 and all(isinstance(v, int) for v in p)
                    for p in raw_points
                )
            ):
                raise HTTPException(
                    422, detail="slot_points phai la danh sach 8 cap so nguyen [x, y]"
                )
            points = [(p[0], p[1]) for p in raw_points]
        settings = load_settings(settings_path)
        try:
            write_settings_farm_skills(settings_path, skills)
            if points is not None:
                write_coordinates_skill_slots(settings.paths.coordinates_file, points)
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        log.info(
            "farm skill settings saved",
            enabled=skills.enabled,
            slots=skills.slots,
            heal_slot=skills.heal_slot,
        )
        return {"status": "saved"}

    # -- static frontend ---------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # no-cache: trình duyệt phải hỏi lại server mỗi lần tải — tránh người
        # vận hành dùng bản HTML cũ sau khi app được cập nhật (sự cố 27/10)
        return FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
