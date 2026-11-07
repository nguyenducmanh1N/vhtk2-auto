"""FastAPI backend of the management app (phase D1).

Thin shell over the YAML configs: every read goes through the same pydantic
loaders the tool itself uses, every write through ``config_writer`` (validated
first, comments preserved). No game/business logic lives here.

The server binds to localhost only; it is started by ``python -m vhtk.ui``.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from vhtk.config import (
    ACCOUNT_FARM_FIELDS,
    AccountConfig,
    AccountFarm,
    ConfigError,
    FarmSettings,
    FarmSkillSettings,
    SafetySettings,
    account_farm_overrides,
    load_account,
    load_coordinates,
    load_settings,
    merge_account_farm,
)
from vhtk.config_writer import (
    write_account,
    write_coordinates_skill_slots,
    write_settings_account_defaults,
    write_settings_farm,
    write_settings_farm_skills,
    write_settings_safety,
)
from vhtk.core.errors import VhtkError
from vhtk.core.ldplayer import LDPlayerManager
from vhtk.fleet import (
    DesiredState,
    FleetController,
    OpLocks,
    SystemOp,
    Watchdog,
    device_phase,
)
from vhtk.mapstore import MapStore
from vhtk.mapstore import maps_dir as store_maps_dir
from vhtk.orchestrator import OrchestratorError, TransferCoordinator, WorkerSupervisor
from vhtk.tasks.navigate import map_slug

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


def create_app(
    settings_path: Path = Path("config/settings.yaml"),
    supervisor_factory: Callable[[], WorkerSupervisor] | None = None,
    start_coordinator: bool = True,
    background_ops: bool = True,
) -> FastAPI:
    """Build the FastAPI app bound to one settings file.

    Args:
        settings_path: the settings.yaml this server reads/writes.
        supervisor_factory: WorkerSupervisor builder override (tests); the
            default builds one per request from the CURRENT settings, with an
            LDPlayerManager only when ``orchestrator.manage_instances`` is on.
        start_coordinator: run the background threads — W3 transfer
            coordinator (when ``transfer.enabled``) and the fleet watchdog
            (tests pass False and drive ``app.state.watchdog_tick`` by hand).
        background_ops: run system-wide ops ("Chạy tất cả"...) in a thread;
            tests pass False to run them inline.
    """
    app = FastAPI(title="VHTK2 Auto Tool — Quản lý", docs_url=None, redoc_url=None)

    def default_supervisor() -> WorkerSupervisor:
        settings = load_settings(settings_path)
        ld = (
            LDPlayerManager(
                settings.ldplayer.ldconsole_path,
                copy_timeout_s=settings.orchestrator.clone_timeout,
            )
            if settings.orchestrator.manage_instances
            else None
        )
        return WorkerSupervisor(settings, settings_path, ld=ld)

    make_supervisor = supervisor_factory or default_supervisor

    # -- fleet: khoá thao tác từng máy, trạng thái "muốn chạy", watchdog --------
    try:
        _boot = load_settings(settings_path)
        _orch = _boot.orchestrator
    except ConfigError:
        from vhtk.config import OrchestratorSettings

        _orch = OrchestratorSettings()
    locks = OpLocks()
    desired = DesiredState(_orch.workers_dir / "desired.json")
    watchdog = Watchdog(_orch.watchdog)
    fleet = FleetController(
        locks, desired, watchdog, make_supervisor,
        lambda: load_settings(settings_path).orchestrator.max_concurrent_instances,
    )
    app.state.locks, app.state.desired, app.state.watchdog = locks, desired, watchdog
    app.state.fleet = fleet

    def run_locked(stem: str, op: str, fn: Callable[[], Any]) -> Any:
        """One lifecycle op per device; a second click while busy -> 409."""
        if fleet.busy():
            raise HTTPException(409, detail="dang chay thao tac toan he thong, cho xong")
        busy = locks.try_begin(stem, op)
        if busy is not None:
            raise HTTPException(409, detail=f"{stem}: dang {busy}, cho xong")
        try:
            return fn()
        finally:
            locks.end(stem)

    @app.exception_handler(ConfigError)
    def config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
        # settings.yaml/schema lệch nhau (vd server chạy code cũ) -> báo rõ
        # thay vì 500 trống trơn
        log.error("config error while serving", detail=str(exc))
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(VhtkError)
    def device_error_handler(request: Request, exc: VhtkError) -> JSONResponse:
        # ldconsole/adb lỗi (vd clone thất bại) -> trả nội dung lỗi cho GUI,
        # không để lọt thành 500 text/plain ("Lỗi không xác định")
        log.error("tool error while serving", path=request.url.path, detail=str(exc))
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    def accounts_dir() -> Path:
        # đọc lại mỗi lần để tôn trọng thay đổi settings giữa chừng
        return load_settings(settings_path).paths.accounts_dir

    def account_path(stem: str) -> Path:
        if not _STEM_RE.fullmatch(stem) or stem == "example":
            raise HTTPException(400, detail=f"ten file khong hop le: {stem!r}")
        return accounts_dir() / f"{stem}.yaml"

    def farm_defaults() -> AccountFarm | None:
        """``account_defaults.farm`` of settings.yaml; None = legacy (no section)."""
        return load_settings(settings_path).farm_defaults

    def account_payload(stem: str, path: Path) -> dict[str, Any]:
        """Merged account + which farm fields the file sets itself (D1b).

        Without ``account_defaults`` every farm field counts as "riêng" so
        the GUI shows the legacy all-editable form.
        """
        defaults = farm_defaults()
        acc = load_account(path, defaults)
        overrides = account_farm_overrides(path) if defaults else list(ACCOUNT_FARM_FIELDS)
        return {
            "stem": stem,
            **acc.model_dump(mode="json"),
            "farm_overrides": overrides,
            "farm_defaults": defaults.model_dump(mode="json") if defaults else None,
        }

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
                acc = load_account(f, farm_defaults())
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
            return account_payload(stem, path)
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
        serial = load_account(path, farm_defaults()).ldplayer.adb_serial
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
        """Save an account; ``farm`` is merged over the shared defaults.

        ``farm_overrides`` (optional list of field names) says which farm
        fields are the account's OWN; the rest are dropped from the file so
        the shared value applies. Absent = every field present in ``farm``.
        """
        path = account_path(stem)
        body.pop("stem", None)
        body.pop("farm_defaults", None)
        wanted = body.pop("farm_overrides", None)
        if wanted is not None and (
            not isinstance(wanted, list) or not set(wanted) <= set(ACCOUNT_FARM_FIELDS)
        ):
            raise HTTPException(422, detail="farm_overrides phai la danh sach ten truong farm")
        raw_farm = body.get("farm") or {}
        if not isinstance(raw_farm, dict):
            raise HTTPException(422, detail="farm phai la mot mapping")
        if wanted is not None:
            raw_farm = {k: v for k, v in raw_farm.items() if k in wanted}
        defaults = farm_defaults()
        try:
            merged = merge_account_farm(raw_farm, defaults, path)
            account = AccountConfig.model_validate({**body, "farm": merged})
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(422, detail=_validation_detail(exc)) from exc
        overrides: set[str] | None = None
        if defaults is not None:
            overrides = (
                set(wanted) if wanted is not None
                else {k for k, v in raw_farm.items() if v is not None}
            )
        write_account(
            path, account, template=accounts_dir() / EXAMPLE_NAME, farm_overrides=overrides
        )
        log.info("account saved", stem=stem, farm_overrides=sorted(overrides or []))
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
                defaults = farm_defaults()
                acc = load_account(example, defaults)
                # acc mới: mặc định mọi trường farm dùng chung
                return {
                    **acc.model_dump(mode="json"),
                    "farm_overrides": [] if defaults else list(ACCOUNT_FARM_FIELDS),
                    "farm_defaults": defaults.model_dump(mode="json") if defaults else None,
                }
            except ConfigError:
                pass
        raise HTTPException(404, detail="example.yaml khong doc duoc")

    # -- farm maps (các map có đủ template để chạy) -------------------------------

    @app.get("/api/farm-maps")
    def farm_maps() -> list[dict[str, str]]:
        """Maps selectable for farming: every NAMED map in the harvested map
        store (``data/maps/<version>``, Navigator dò bằng crop đã quét) plus
        maps with a hand-captured ``row_``/``hud_name_`` template pair (display
        names from ``display_name`` in the template manifest)."""
        settings = load_settings(settings_path)
        by_slug: dict[str, str] = {}
        for name in MapStore(store_maps_dir(settings.templates.version)).names():
            by_slug[map_slug(name)] = name
        map_dir = settings.templates.root / "map"
        manifest: dict[str, Any] = {}
        mf = settings.templates.root / "manifest.yaml"
        if mf.is_file():
            raw = yaml.safe_load(mf.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                manifest = raw
        if map_dir.is_dir():
            for row in sorted(map_dir.glob("row_*.png")):
                slug = row.stem.removeprefix("row_")
                if not (map_dir / f"hud_name_{slug}.png").is_file():
                    continue  # thiếu nửa cặp template -> chỉ dùng được qua kho
                meta = manifest.get(f"map/row_{slug}")
                disp = meta.get("display_name") if isinstance(meta, dict) else None
                # tên hiển thị trong manifest template thắng tên kho (có dấu chuẩn hơn)
                if disp or slug not in by_slug:
                    by_slug[slug] = str(disp) if disp else slug.replace("_", " ")
        return sorted(
            ({"slug": s, "name": n} for s, n in by_slug.items()),
            key=lambda e: e["name"],
        )

    # -- map library (kho ảnh map đã quét bằng tools/harvest_maps.py) --------------

    def maps_dir() -> Path:
        return store_maps_dir(load_settings(settings_path).templates.version)

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

    # -- workers (W2: orchestrator API — mỗi acc một tiến trình worker riêng) ------

    def _checked_stem(stem: str) -> str:
        if not account_path(stem).is_file():
            raise HTTPException(404, detail=f"khong tim thay tai khoan {stem!r}")
        return stem

    @app.get("/api/workers")
    def list_workers() -> list[dict[str, Any]]:
        """Every account: worker process + device state + watchdog view.

        ``phase`` (fleet.device_phase) is what the card shows; buttons follow
        it, so a device that is up but has no worker no longer offers
        "Mở & đăng nhập", and a device mid-boot offers nothing.
        """
        sup = make_supervisor()
        rows: list[dict[str, Any]] = []
        folder = accounts_dir()
        if not folder.is_dir():
            return rows
        stems = [f.stem for f in sorted(folder.glob("*.yaml")) if f.name != EXAMPLE_NAME]
        try:
            instances = sup.instance_states(stems)
        except Exception as exc:  # truy vấn máy lỗi không được làm hỏng cả trang
            log.warning("instance state query failed", error=str(exc))
            instances = {s: None for s in stems}
        for stem in stems:
            f = folder / f"{stem}.yaml"
            row = sup.worker_status(stem)
            try:
                acc = load_account(f, farm_defaults())
                row.update(name=acc.name, role=acc.role,
                           ldplayer_index=acc.ldplayer.index,
                           adb_serial=acc.ldplayer.adb_serial)
            except ConfigError as exc:
                row["error"] = str(exc)
            health = watchdog.view(stem)
            want = desired.get(stem)
            op = locks.current(stem) or (fleet.status.op if fleet.busy() else None)
            row.update(
                instance=instances.get(stem),
                op=op,
                desired=want,
                health=health,
                phase=device_phase(
                    op=op, running=bool(row.get("running")), instance=instances.get(stem),
                    health=health, state=row.get("state") or {}, desired=want,
                ),
            )
            rows.append(row)
        return rows

    def _want_running(stem: str) -> None:
        # người bấm chạy = muốn chạy; quên lịch sử hỏng cũ của watchdog
        desired.set(stem, "running")
        watchdog.reset(stem)

    @app.post("/api/workers/{stem}/start")
    def start_worker(stem: str) -> dict[str, Any]:
        _checked_stem(stem)
        _want_running(stem)
        try:
            pid = run_locked(stem, "start", lambda: make_supervisor().start(stem))
        except OrchestratorError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        return {"status": "started", "stem": stem, "pid": pid}

    @app.post("/api/workers/{stem}/stop")
    def stop_worker(stem: str) -> dict[str, Any]:
        _checked_stem(stem)
        # TRƯỚC khi dừng: watchdog không được hồi sinh máy người vừa bấm dừng
        desired.set(stem, "stopped")
        watchdog.reset(stem)
        run_locked(stem, "stop", lambda: make_supervisor().stop(stem))
        return {"status": "stopped", "stem": stem}

    @app.post("/api/workers/{stem}/restart")
    def restart_worker(stem: str) -> dict[str, Any]:
        _checked_stem(stem)
        _want_running(stem)
        try:
            pid = run_locked(stem, "restart", lambda: make_supervisor().restart(stem))
        except OrchestratorError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        return {"status": "restarted", "stem": stem, "pid": pid}

    @app.post("/api/workers/{stem}/provision")
    def provision_worker(stem: str) -> dict[str, Any]:
        """Clone the template LDPlayer instance for this account and start it
        (Windows only: needs ``orchestrator.manage_instances`` + template index)."""
        _checked_stem(stem)
        _want_running(stem)
        try:
            pid = run_locked(stem, "provision", lambda: make_supervisor().provision(stem))
        except OrchestratorError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        return {"status": "provisioned", "stem": stem, "pid": pid}

    # -- toàn hệ thống: Chạy / Chạy lại / Dừng tất cả --------------------------------

    def _fleet_stems() -> tuple[list[str], list[str]]:
        """(farm accounts to start, every account to stop). Main is on-demand
        (CLAUDE.md) — never started by "Chạy tất cả", but stopped by "Dừng"."""
        farms: list[str] = []
        every: list[str] = []
        folder = accounts_dir()
        for f in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
            if f.name == EXAMPLE_NAME:
                continue
            every.append(f.stem)
            try:
                if load_account(f, farm_defaults()).role == "farm":
                    farms.append(f.stem)
            except ConfigError:
                continue
        return farms, every

    @app.get("/api/system")
    def system_status() -> dict[str, Any]:
        return fleet.status.view()

    @app.post("/api/system/{op}")
    def system_op(op: str) -> dict[str, Any]:
        ops: dict[str, SystemOp] = {
            "start": "start_all", "stop": "stop_all", "restart": "restart_all",
        }
        if op not in ops:
            raise HTTPException(404, detail=f"thao tac khong ho tro: {op!r}")
        farms, every = _fleet_stems()
        kind = ops[op]
        total = {"start_all": len(farms), "stop_all": len(every),
                 "restart_all": len(every) + len(farms)}[kind]
        if not fleet.begin(kind, total):
            raise HTTPException(409, detail="dang chay thao tac toan he thong, cho xong")
        log.warning("system op requested", op=kind, farms=farms)
        if background_ops:
            threading.Thread(target=fleet.run, args=(kind, farms, every),
                             name=f"fleet-{kind}", daemon=True).start()
        else:
            fleet.run(kind, farms, every)
        return {"status": "accepted", "op": kind, "total": total}

    def watchdog_tick() -> list[str]:
        """One watchdog pass over every account; returns the actions taken.

        Recovery actions run in their own thread holding the account's op
        lock (a reboot takes minutes), so other accounts keep being watched.
        """
        if fleet.busy():
            return []
        taken: list[str] = []
        sup = make_supervisor()
        for stem in _fleet_stems()[1]:
            st = sup.worker_status(stem)
            action = watchdog.decide(
                stem,
                desired=desired.get(stem),
                busy=locks.current(stem) is not None,
                alive=bool(st.get("running")),
                state=st.get("state") or {},
            )
            if action is None or locks.try_begin(stem, f"watchdog:{action.kind}") is not None:
                continue
            taken.append(f"{stem}:{action.kind}")

            def _run(a: Any = action, s: str = stem) -> None:
                try:
                    watchdog.execute(a, make_supervisor())
                except Exception as exc:
                    log.error("watchdog action failed", stem=s, kind=a.kind, error=str(exc))
                finally:
                    locks.end(s)

            if background_ops:
                threading.Thread(target=_run, name=f"watchdog-{stem}", daemon=True).start()
            else:
                _run()
        return taken

    app.state.watchdog_tick = watchdog_tick

    # -- runner (tương thích GUI cũ — giờ chạy trên registry/supervisor) -----------

    @app.get("/api/runner")
    def runner_status() -> dict[str, Any]:
        sup = make_supervisor()
        stems = sup.registry.live_stems()
        pids = [p for s in stems if (i := sup.registry.info(s)) and (p := i.pid)]
        return {"running": bool(stems), "pids": pids, "stems": stems}

    @app.post("/api/runner/restart")
    def runner_restart(body: dict[str, Any]) -> dict[str, Any]:
        stem = _checked_stem(str(body.get("account", "")))
        _want_running(stem)
        try:
            pid = run_locked(stem, "restart", lambda: make_supervisor().restart(stem))
        except OrchestratorError as exc:
            raise HTTPException(409, detail=str(exc)) from exc
        return {"status": "restarted", "pid": pid, "account": stem}

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

    # -- cài đặt chung (D1b): farm mặc định cho mọi acc + thông số farm toàn cục ----

    @app.get("/api/settings/account-defaults")
    def get_account_defaults() -> dict[str, Any]:
        d = farm_defaults()
        return {"configured": d is not None, "farm": d.model_dump(mode="json") if d else None}

    @app.put("/api/settings/account-defaults")
    def put_account_defaults(body: dict[str, Any]) -> dict[str, Any]:
        """Save the shared farm defaults — refused if any account would break.

        An account that overrides only one side of a cross-field rule (vd
        riêng hp_resume=90, chung đổi hp_retreat=95) would fail to load
        afterwards; better to reject here and name it.
        """
        raw = body.get("farm", body)
        try:
            farm = AccountFarm.model_validate(raw)
        except ValidationError as exc:
            raise HTTPException(422, detail=_validation_detail(exc)) from exc
        broken: list[str] = []
        folder = accounts_dir()
        for f in sorted(folder.glob("*.yaml")) if folder.is_dir() else []:
            if f.name == EXAMPLE_NAME:
                continue
            try:
                load_account(f, farm)
            except ConfigError as exc:
                broken.append(f"{f.stem}: {exc.args[0].splitlines()[-1].strip()}")
        if broken:
            raise HTTPException(
                422, detail="cai dat chung moi lam hong tai khoan: " + "; ".join(broken)
            )
        try:
            write_settings_account_defaults(settings_path, farm)
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        log.info("account defaults saved", map=farm.map, hp_retreat=farm.hp_retreat_percent)
        return {"status": "saved"}

    @app.get("/api/settings/farm")
    def get_farm_settings() -> dict[str, Any]:
        return load_settings(settings_path).farm.model_dump(mode="json")

    @app.put("/api/settings/farm")
    def put_farm_settings(body: dict[str, Any]) -> dict[str, str]:
        """Update global farm knobs; ``skills`` has its own endpoint and is ignored."""
        body.pop("skills", None)
        current = load_settings(settings_path).farm.model_dump(mode="json")
        try:
            farm = FarmSettings.model_validate({**current, **body})
        except ValidationError as exc:
            raise HTTPException(422, detail=_validation_detail(exc)) from exc
        try:
            write_settings_farm(settings_path, farm)
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
        log.info("farm settings saved")
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

    # -- W3: vòng điều phối chuyển vàng (chạy nền trong tiến trình server) -------
    coordinator: TransferCoordinator | None = None

    @app.get("/api/transfer")
    def transfer_status() -> dict[str, Any]:
        settings = load_settings(settings_path)
        sess = coordinator.session if coordinator else None
        return {
            "enabled": settings.transfer.enabled,
            "currency": settings.transfer.currency,
            "main_stem": settings.transfer.main_stem,
            "pending": sorted(coordinator.scheduler.pending) if coordinator else [],
            "session": (
                {"farm": sess.farm_stem, "paused": sess.paused} if sess is not None else None
            ),
        }

    if start_coordinator and _orch.watchdog.enabled:

        def _watch(interval: float) -> None:
            while True:
                try:
                    acted = watchdog_tick()
                    if acted:
                        log.warning("watchdog acted", actions=acted)
                except Exception as exc:  # vòng nền không bao giờ được chết im lặng
                    log.error("watchdog tick crashed", error=str(exc))
                time.sleep(interval)

        threading.Thread(target=_watch, args=(_orch.watchdog.poll_interval,),
                         name="fleet-watchdog", daemon=True).start()
        log.info("fleet watchdog started", interval=_orch.watchdog.poll_interval)

    if start_coordinator:
        try:
            boot = load_settings(settings_path)
        except ConfigError as exc:
            log.error("transfer coordinator not started: config error", detail=str(exc))
            boot = None
        if boot is not None and boot.transfer.enabled:
            coordinator = TransferCoordinator(boot, make_supervisor())

            def _loop(coord: TransferCoordinator, interval: float) -> None:
                while True:
                    try:
                        actions = coord.tick()
                        if actions:
                            log.info("transfer coordinator", actions=actions)
                    except Exception as exc:  # vòng nền không bao giờ được chết im lặng
                        log.error("transfer coordinator crashed tick", error=str(exc))
                    time.sleep(interval)

            threading.Thread(
                target=_loop,
                args=(coordinator, boot.transfer.poll_interval),
                name="transfer-coordinator",
                daemon=True,
            ).start()
            log.info("transfer coordinator started", interval=boot.transfer.poll_interval)
    return app
