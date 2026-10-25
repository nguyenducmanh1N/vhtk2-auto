"""FastAPI backend of the management app (phase D1).

Thin shell over the YAML configs: every read goes through the same pydantic
loaders the tool itself uses, every write through ``config_writer`` (validated
first, comments preserved). No game/business logic lives here.

The server binds to localhost only; it is started by ``python -m vhtk.ui``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from vhtk.config import (
    AccountConfig,
    ConfigError,
    SafetySettings,
    load_account,
    load_settings,
)
from vhtk.config_writer import write_account, write_settings_safety

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

    # -- static frontend ---------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
