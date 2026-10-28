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
    orch_cfg = settings.orchestrator.model_copy(
        update={"workers_dir": tmp_path / "workers", **orch}
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


def write_test_account(tmp_path: Path, stem: str, index: int = 1) -> Path:
    from vhtk.config import AccountConfig

    path = tmp_path / "accounts" / f"{stem}.yaml"
    account = AccountConfig.model_validate(
        {
            "name": stem,
            "role": "farm",
            "ldplayer": {"index": index, "adb_serial": f"emulator-{5554 + 2 * index}"},
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


def fake_ld(instances: dict[int, bool], clone_index: int | None = None) -> LDPlayerManager:
    """LDPlayerManager over a fake ldconsole: list2/isrunning/launch/copy."""

    def runner(args: list[str]) -> str:
        cmd = args[0]
        if cmd == "list2":
            return "\n".join(
                f"{i},inst{i},0,0,{int(running)},1,1" for i, running in sorted(instances.items())
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
            return ""
        if cmd == "adb":  # get-serialno
            return f"emulator-{5554 + 2 * int(args[2])}\n"
        raise AssertionError(f"unexpected ldconsole command: {args}")

    return LDPlayerManager("ldconsole.exe", runner=runner, adb_serials=list)


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
    path = write_test_account(tmp_path, "farm09", index=1)
    instances = {0: False}
    h = Harness(tmp_path, settings, ld=fake_ld(instances, clone_index=7))
    h.supervisor.provision("farm09")
    from vhtk.config import load_account

    acc = load_account(path)
    assert acc.ldplayer.index == 7  # index mới ghi vào YAML
    assert h.registry.alive("farm09")


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
