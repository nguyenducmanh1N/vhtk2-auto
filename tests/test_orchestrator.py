"""Tests for the W2 orchestrator: supervisor lifecycle and main scheduler.

Everything runs on fakes (no ldconsole, no adb, no real processes): the
supervisor gets an injected spawner/boot probe/process stopper and a registry
with an injected cmdline reader; LDPlayerManager gets a fake runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vhtk.config import Settings, load_settings
from vhtk.config_writer import write_account
from vhtk.core.errors import LDConsoleError
from vhtk.core.ldplayer import LDPlayerManager
from vhtk.orchestrator import Action, MainScheduler, OrchestratorError, WorkerSupervisor
from vhtk.registry import WorkerRegistry

SETTINGS_PATH = Path(__file__).parent.parent / "config" / "settings.yaml"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_settings(tmp_path: Path, **orch: object) -> Settings:
    settings = load_settings(SETTINGS_PATH)
    # Cố định giá trị mặc định cho test, không phụ thuộc settings.yaml của máy đang chạy
    orch_cfg = settings.orchestrator.model_copy(
        update={
            "workers_dir": tmp_path / "workers",
            "manage_instances": False,
            "template_instance_index": None,
            **orch,
        }
    )
    return settings.model_copy(
        update={
            "orchestrator": orch_cfg,
            "paths": settings.paths.model_copy(update={"accounts_dir": tmp_path / "accounts"}),
            "safety": settings.safety.model_copy(
                update={"kill_switch_file": str(tmp_path / "STOP")}
            ),
        }
    )


def write_test_account(tmp_path: Path, stem: str, index: int | None = 1) -> Path:
    from vhtk.config import AccountConfig

    path = tmp_path / "accounts" / f"{stem}.yaml"
    account = AccountConfig.model_validate(
        {
            "name": stem,
            "role": "farm",
            "ldplayer": {"index": index, "adb_serial": f"emulator-{5554 + 2 * (index or 0)}"},
            "login": {"server": "S9", "username": "u", "password": "p"},
            "farm": {"map": "Tuyền Châu", "gold_transfer_threshold": 0},
        }
    )
    write_account(path, account)
    return path


class Harness:
    """Supervisor with every external effect captured."""

    def __init__(self, tmp_path: Path, settings: Settings, ld: LDPlayerManager | None = None):
        self.spawned: list[tuple[str, Path]] = []
        self.stopped: list[tuple[int, bool]] = []
        self.clock = FakeClock()
        self._pids: dict[str, int] = {}
        self._next_pid = 100
        # cmdline giả: PID "sống" chừng nào chưa bị stop cứng và stem chưa
        # "tự thoát" qua kill_on_stop_file
        self.live_pids: dict[int, list[str]] = {}
        self.registry = WorkerRegistry(
            settings.orchestrator.workers_dir, cmdline=lambda pid: self.live_pids.get(pid)
        )
        self.kill_switch_base = Path(settings.safety.kill_switch_file)
        self.obeys_kill_switch = True

        def spawn(stem: str, account: Path, settings_path: Path) -> int:
            pid = self._next_pid
            self._next_pid += 1
            self.spawned.append((stem, account))
            self.live_pids[pid] = ["python", "-m", "vhtk.worker", "--account", str(account)]
            self._pids[stem] = pid
            return pid

        def sleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            # worker ngoan: thấy STOP.<stem> là thoát
            if self.obeys_kill_switch:
                for stem, pid in list(self._pids.items()):
                    stop = self.kill_switch_base.with_name(f"{self.kill_switch_base.name}.{stem}")
                    if stop.exists():
                        self.live_pids.pop(pid, None)

        def proc_stop(pid: int, hard: bool) -> None:
            self.stopped.append((pid, hard))
            self.live_pids.pop(pid, None)

        self.supervisor = WorkerSupervisor(
            settings,
            SETTINGS_PATH,
            ld=ld,
            registry=self.registry,
            spawn=spawn,
            boot_probe=lambda serial: True,
            proc_stop=proc_stop,
            clock=self.clock,
            sleep=sleep,
        )


def test_start_spawns_and_registers(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)  # manage_instances=False: không đụng ldconsole
    write_test_account(tmp_path, "farm01")
    h = Harness(tmp_path, settings)
    pid = h.supervisor.start("farm01")
    assert h.spawned == [("farm01", tmp_path / "accounts" / "farm01.yaml")]
    assert h.registry.alive("farm01")
    assert h.supervisor.worker_status("farm01")["pid"] == pid


def test_start_unknown_or_running_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_test_account(tmp_path, "farm01")
    h = Harness(tmp_path, settings)
    with pytest.raises(OrchestratorError, match="unknown account"):
        h.supervisor.start("khongco")
    h.supervisor.start("farm01")
    with pytest.raises(OrchestratorError, match="already running"):
        h.supervisor.start("farm01")


def test_start_clears_stale_per_account_stop_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_test_account(tmp_path, "farm01")
    stale = tmp_path / "STOP.farm01"
    stale.touch()
    h = Harness(tmp_path, settings)
    h.supervisor.start("farm01")
    assert not stale.exists()


def test_stop_uses_kill_switch_then_cleans_up(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_test_account(tmp_path, "farm01")
    h = Harness(tmp_path, settings)
    h.supervisor.start("farm01")
    h.supervisor.stop("farm01")
    # worker ngoan thoát qua kill-switch -> không cần terminate/kill
    assert h.stopped == []
    assert not h.registry.alive("farm01")
    assert not (tmp_path / "STOP.farm01").exists()  # dọn file sau khi dừng


def test_stop_escalates_when_worker_hangs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, stop_timeout=5.0)
    write_test_account(tmp_path, "farm01")
    h = Harness(tmp_path, settings)
    h.obeys_kill_switch = False  # worker treo, lờ kill-switch
    pid = h.supervisor.start("farm01")
    h.supervisor.stop("farm01")
    assert (pid, False) in h.stopped  # terminate trước
    assert not h.registry.alive("farm01")


def test_stop_only_touches_one_worker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_test_account(tmp_path, "farm01", index=1)
    write_test_account(tmp_path, "farm02", index=2)
    h = Harness(tmp_path, settings)
    h.supervisor.start("farm01")
    h.supervisor.start("farm02")
    h.supervisor.stop("farm01")
    assert not h.registry.alive("farm01")
    assert h.registry.alive("farm02")


def test_restart_stops_then_starts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    write_test_account(tmp_path, "farm01")
    h = Harness(tmp_path, settings)
    pid1 = h.supervisor.start("farm01")
    pid2 = h.supervisor.restart("farm01")
    assert pid2 != pid1
    assert h.registry.alive("farm01")


def template_vms(tmp_path: Path, index: int = 0, size: int = 100) -> Path:
    """Fake LDPlayer ``vms`` dir holding the template instance's disk."""
    disk = tmp_path / "vms" / f"leidian{index}"
    disk.mkdir(parents=True)
    (disk / "data.vmdk").write_bytes(b"\0" * size)
    return tmp_path / "vms"


def fake_ld(
    instances: dict[int, bool],
    clone_index: int | None = None,
    *,
    vms_dir: Path | None = None,
    free: int = 10**15,
) -> LDPlayerManager:
    """LDPlayerManager over a fake ldconsole: list2/isrunning/launch/copy."""
    names: dict[int, str] = {}

    def runner(args: list[str]) -> str:
        cmd = args[0]
        if cmd == "list2":
            return "\n".join(
                f"{i},{names.get(i, f'inst{i}')},0,0,{int(running)},1,1"
                for i, running in sorted(instances.items())
            )
        if cmd == "isrunning":
            return "running" if instances.get(int(args[2]), False) else "stop"
        if cmd == "launch":
            instances[int(args[2])] = True
            return ""
        if cmd == "quit":
            instances[int(args[2])] = False
            return ""
        if cmd == "copy":
            assert clone_index is not None
            instances[clone_index] = False
            names[clone_index] = args[2]  # LDPlayer đặt tên thật khi copy xong
            return ""
        if cmd == "adb":  # get-serialno
            return f"emulator-{5554 + 2 * int(args[2])}\n"
        raise AssertionError(f"unexpected ldconsole command: {args}")

    return LDPlayerManager(
        "ldconsole.exe",
        runner=runner,
        adb_serials=list,
        vms_dir=vms_dir,
        free_bytes=lambda path: free,
    )


def test_managed_start_launches_instance_and_fixes_serial(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, manage_instances=True, boot_timeout=30.0)
    path = write_test_account(tmp_path, "farm01", index=3)
    # YAML cố tình ghi serial sai để supervisor phải sửa lại
    text = path.read_text(encoding="utf-8").replace("emulator-5560", "emulator-9999")
    path.write_text(text, encoding="utf-8")
    instances = {3: False}
    h = Harness(tmp_path, settings, ld=fake_ld(instances))
    h.supervisor.start("farm01")
    assert instances[3] is True  # đã launch
    from vhtk.config import load_account

    assert load_account(path).ldplayer.adb_serial == "emulator-5560"  # serial ghi lại YAML


def test_managed_stop_quits_instance(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, manage_instances=True)
    write_test_account(tmp_path, "farm01", index=3)
    instances = {3: False}
    h = Harness(tmp_path, settings, ld=fake_ld(instances))
    h.supervisor.start("farm01")
    h.supervisor.stop("farm01")
    assert instances[3] is False


def test_provision_clones_template_and_starts(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, manage_instances=True, template_instance_index=0
    )
    path = write_test_account(tmp_path, "farm09", index=None)  # acc chưa có máy
    instances = {0: False}
    ld = fake_ld(instances, clone_index=7, vms_dir=template_vms(tmp_path))
    h = Harness(tmp_path, settings, ld=ld)
    h.supervisor.provision("farm09")
    from vhtk.config import load_account

    acc = load_account(path)
    assert acc.ldplayer.index == 7  # index mới ghi vào YAML
    assert h.registry.alive("farm09")


def test_provision_keeps_disk_headroom_from_config(tmp_path: Path) -> None:
    # ổ C còn 7GB mà mỗi bản clone ~8.7GB (01/11): clone tiếp sẽ làm đầy ổ và
    # có thể hỏng đĩa ảo của các máy đang chạy
    settings = make_settings(
        tmp_path, manage_instances=True, template_instance_index=0, clone_headroom_gb=1.0
    )
    path = write_test_account(tmp_path, "farm09", index=None)
    instances = {0: False}
    ld = fake_ld(
        instances, clone_index=7, vms_dir=template_vms(tmp_path, size=100), free=100 + 2**30 - 1
    )
    h = Harness(tmp_path, settings, ld=ld)
    with pytest.raises(LDConsoleError, match="not enough disk space"):
        h.supervisor.provision("farm09")
    from vhtk.config import load_account

    assert instances == {0: False}  # không copy
    assert load_account(path).ldplayer.index is None
    assert h.spawned == []


def test_provision_refuses_account_that_already_has_instance(tmp_path: Path) -> None:
    # lỗi thực tế 28/10: bấm "Cấp máy mới" cho acc đang dùng instance 0
    # -> clone ~5GB thừa + instance mồ côi
    settings = make_settings(tmp_path, manage_instances=True, template_instance_index=0)
    path = write_test_account(tmp_path, "farm01", index=0)
    instances = {0: False}
    h = Harness(tmp_path, settings, ld=fake_ld(instances))  # copy -> AssertionError
    with pytest.raises(OrchestratorError, match="already has instance 0"):
        h.supervisor.provision("farm01")
    assert instances == {0: False}  # không clone gì
    from vhtk.config import load_account

    assert load_account(path).ldplayer.index == 0


def test_managed_start_rejects_account_without_instance(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, manage_instances=True)
    write_test_account(tmp_path, "farm02", index=None)
    h = Harness(tmp_path, settings, ld=fake_ld({0: False}))
    with pytest.raises(OrchestratorError, match="provision it first"):
        h.supervisor.start("farm02")
    assert h.spawned == []
    h.supervisor.stop("farm02")  # dừng acc chưa có máy: không lỗi, không quit gì


def test_provision_requires_config(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)  # manage_instances=False
    write_test_account(tmp_path, "farm01")
    h = Harness(tmp_path, settings)
    with pytest.raises(OrchestratorError, match="manage_instances"):
        h.supervisor.provision("farm01")
    settings2 = make_settings(tmp_path, manage_instances=True)  # thiếu template index
    h2 = Harness(tmp_path, settings2, ld=fake_ld({0: False}))
    with pytest.raises(OrchestratorError, match="template_instance_index"):
        h2.supervisor.provision("farm01")


# ---------------------------------------------------------------------------
# MainScheduler
# ---------------------------------------------------------------------------


def test_scheduler_waits_for_batch_window() -> None:
    s = MainScheduler(cfg_max_concurrent=5, batch_window_s=300, main_stem="main")
    s.note_gold_request("farm01", now=0.0)
    assert s.plan(now=100.0, running_farms={"farm01", "farm02"}, main_running=False) == []
    acts = s.plan(now=300.0, running_farms={"farm01", "farm02"}, main_running=False)
    assert acts == [Action(kind="start_main", stem="main")]


def test_scheduler_zero_window_starts_immediately() -> None:
    s = MainScheduler(cfg_max_concurrent=5, batch_window_s=0, main_stem="main")
    s.note_gold_request("farm01", now=10.0)
    acts = s.plan(now=10.0, running_farms={"farm01"}, main_running=False)
    assert acts == [Action(kind="start_main", stem="main")]


def test_scheduler_pauses_an_idle_farm_when_slots_full() -> None:
    s = MainScheduler(cfg_max_concurrent=3, batch_window_s=0, main_stem="main")
    s.note_gold_request("farm01", now=0.0)
    acts = s.plan(now=0.0, running_farms={"farm01", "farm02", "farm03"}, main_running=False)
    # farm trong chuyến (farm01) phải online — tạm dừng farm khác
    assert acts[0].kind == "pause_farm" and acts[0].stem in {"farm02", "farm03"}
    assert acts[-1] == Action(kind="start_main", stem="main")


def test_scheduler_postpones_when_no_pausable_farm() -> None:
    s = MainScheduler(cfg_max_concurrent=2, batch_window_s=0, main_stem="main")
    s.note_gold_request("farm01", now=0.0)
    s.note_gold_request("farm02", now=0.0)
    assert s.plan(now=0.0, running_farms={"farm01", "farm02"}, main_running=False) == []


def test_scheduler_idle_while_main_running_and_clears() -> None:
    s = MainScheduler(cfg_max_concurrent=5, batch_window_s=0, main_stem="main")
    s.note_gold_request("farm01", now=0.0)
    assert s.plan(now=0.0, running_farms=set(), main_running=True) == []
    s.clear(["farm01"])
    assert s.plan(now=0.0, running_farms=set(), main_running=False) == []
    assert s.pending == {}


def test_scheduler_request_idempotent() -> None:
    s = MainScheduler(cfg_max_concurrent=5, batch_window_s=100, main_stem="main")
    s.note_gold_request("farm01", now=0.0)
    s.note_gold_request("farm01", now=50.0)  # không reset mốc đầu
    assert s.pending["farm01"] == 0.0


# ---------------------------------------------------------------------------
# W3: TransferCoordinator — chạy quyết định của scheduler trên supervisor giả
# ---------------------------------------------------------------------------

import json  # noqa: E402

from vhtk.orchestrator import TransferCoordinator  # noqa: E402
from vhtk.worker import state_file  # noqa: E402


def write_main_account(tmp_path: Path) -> Path:
    from vhtk.config import AccountConfig

    path = tmp_path / "accounts" / "main.yaml"
    account = AccountConfig.model_validate(
        {
            "name": "main",
            "role": "main",
            "ldplayer": {"index": 9, "adb_serial": "emulator-5572"},
            "login": {"server": "S9", "username": "m", "password": "p"},
            "farm": {"map": "Tuyền Châu", "gold_transfer_threshold": 0},
        }
    )
    write_account(path, account)
    return path


def transfer_settings(tmp_path: Path, **orch: object) -> Settings:
    settings = make_settings(tmp_path, main_batch_window_seconds=0.0, **orch)
    return settings.model_copy(
        update={
            "transfer": settings.transfer.model_copy(
                update={"enabled": True, "main_stem": "main", "retry_delay_seconds": 900.0}
            )
        }
    )


def set_state(settings: Settings, stem: str, **fields: object) -> None:
    sf = state_file(settings.orchestrator.workers_dir, stem)
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps({"stem": stem, **fields}), encoding="utf-8")


def test_coordinator_disabled_does_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    h = Harness(tmp_path, settings)
    coord = TransferCoordinator(settings, h.supervisor, clock=h.clock)
    assert coord.tick() == []


def test_coordinator_opens_session_for_requesting_farm(tmp_path: Path) -> None:
    settings = transfer_settings(tmp_path)
    write_test_account(tmp_path, "farm01", index=1)
    write_main_account(tmp_path)
    h = Harness(tmp_path, settings)
    h.supervisor.start("farm01")
    coord = TransferCoordinator(settings, h.supervisor, clock=h.clock)
    assert coord.tick() == []  # chưa có cờ -> không làm gì
    set_state(settings, "farm01", gold_request=True)
    actions = coord.tick()
    assert actions == ["start_main:farm01"]
    workers = settings.orchestrator.workers_dir
    barrier = json.loads((workers / "transfer.json").read_text(encoding="utf-8"))
    assert barrier["farm_stem"] == "farm01" and barrier["main_stem"] == "main"
    assert (workers / "farm01.transfer.json").is_file()
    assert h.registry.alive("main")
    assert coord.session is not None and coord.session.farm_stem == "farm01"
    # đang có chuyến: không mở thêm dù farm khác giơ cờ
    assert coord.tick() == []


def test_coordinator_closes_session_and_backs_off_on_failure(tmp_path: Path) -> None:
    settings = transfer_settings(tmp_path)
    write_test_account(tmp_path, "farm01", index=1)
    write_main_account(tmp_path)
    h = Harness(tmp_path, settings)
    h.supervisor.start("farm01")
    set_state(settings, "farm01", gold_request=True)
    coord = TransferCoordinator(settings, h.supervisor, clock=h.clock)
    coord.tick()
    workers = settings.orchestrator.workers_dir
    barrier_path = workers / "transfer.json"
    data = json.loads(barrier_path.read_text(encoding="utf-8"))
    data["farm"] = {"phase": "failed", "reason": "partner timeout"}
    data["main"] = {"phase": "failed"}
    barrier_path.write_text(json.dumps(data), encoding="utf-8")
    actions = coord.tick()
    assert actions[0] == "close:farm01:fail" and "stop_main:main" in actions
    assert not (workers / "farm01.transfer.json").exists()
    assert not h.registry.alive("main")
    assert coord.session is None
    # cờ vẫn còn nhưng đang trong thời gian giãn -> không mở chuyến mới ngay
    assert coord.tick() == []
    h.clock.now += 901
    assert coord.tick() == ["start_main:farm01"]


def test_coordinator_pauses_idle_farm_when_slots_are_full(tmp_path: Path) -> None:
    settings = transfer_settings(tmp_path, max_concurrent_instances=2)
    write_test_account(tmp_path, "farm01", index=1)
    write_test_account(tmp_path, "farm02", index=2)
    write_main_account(tmp_path)
    h = Harness(tmp_path, settings)
    h.supervisor.start("farm01")
    h.supervisor.start("farm02")
    set_state(settings, "farm01", gold_request=True)
    coord = TransferCoordinator(settings, h.supervisor, clock=h.clock)
    actions = coord.tick()
    assert actions == ["pause_farm:farm02", "start_main:farm01"]
    assert not h.registry.alive("farm02") and h.registry.alive("farm01")
    # main thoát (worker main chỉ sống 1 chuyến) với kết quả đã xác minh
    data = json.loads((settings.orchestrator.workers_dir / "transfer.json").read_text())
    data["farm"] = {"phase": "done", "verified": True}
    data["main"] = {"phase": "done", "verified": True}
    (settings.orchestrator.workers_dir / "transfer.json").write_text(json.dumps(data))
    actions = coord.tick()
    assert actions == ["close:farm01:ok", "stop_main:main", "resume_farm:farm02"]
    assert h.registry.alive("farm02")


def test_coordinator_closes_when_main_worker_is_gone(tmp_path: Path) -> None:
    settings = transfer_settings(tmp_path)
    write_test_account(tmp_path, "farm01", index=1)
    write_main_account(tmp_path)
    h = Harness(tmp_path, settings)
    h.supervisor.start("farm01")
    set_state(settings, "farm01", gold_request=True)
    coord = TransferCoordinator(settings, h.supervisor, clock=h.clock)
    coord.tick()
    main_pid = h.registry.info("main")
    assert main_pid is not None
    h.live_pids.pop(main_pid.pid, None)  # worker main crash/thoát
    actions = coord.tick()
    assert actions[0] == "close:farm01:fail"
    assert coord.session is None
