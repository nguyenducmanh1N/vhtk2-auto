"""Tests for ldconsole output parsing and serial resolution (no ldconsole needed)."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from vhtk.core.errors import LDConsoleError
from vhtk.core.ldplayer import LDPlayerManager, default_serial, parse_list2_line


class FakeRunner:
    """Maps the first ldconsole argument to a canned stdout."""

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        key = args[0]
        if key not in self.responses:
            raise LDConsoleError(f"no canned response for {key}")
        return self.responses[key]


def make_manager(responses: dict[str, str], serials: list[str] | None = None) -> LDPlayerManager:
    return LDPlayerManager(
        "C:/LDPlayer/LDPlayer9/ldconsole.exe",
        runner=FakeRunner(responses),
        adb_serials=lambda: serials or [],
    )


# ---------------------------------------------------------------------------
# list2 parsing
# ---------------------------------------------------------------------------


def test_parse_list2_standard_line() -> None:
    assert parse_list2_line("0,main,12345,67890,1,4321,8765") == (0, "main", True)
    assert parse_list2_line("2,farm02,0,0,0,-1,-1") == (2, "farm02", False)


def test_parse_list2_extended_line_with_resolution_columns() -> None:
    line = "1,farm01,111,222,1,333,444,960,540,160"
    assert parse_list2_line(line) == (1, "farm01", True)


def test_parse_list2_name_containing_comma() -> None:
    assert parse_list2_line("3,farm,ba,111,222,1,333,444") == (3, "farm,ba", True)


@pytest.mark.parametrize(
    "line",
    [
        "",
        "garbage",
        "notanumber,main,1,2,1,3,4",
        "0,main,1,2",  # thiếu cột
        "0,main,1,2,9,3,4",  # android_started không phải 0/1
    ],
)
def test_parse_list2_rejects_bad_lines(line: str) -> None:
    assert parse_list2_line(line) is None


def test_list_instances_skips_bad_lines_and_resolves_running_only() -> None:
    responses = {
        "list2": "0,main,1,2,1,3,4\ngarbage line\n1,farm01,0,0,0,-1,-1\n",
        "adb": "emulator-5554\n",
    }
    mgr = make_manager(responses)
    instances = mgr.list_instances()
    assert len(instances) == 2
    assert instances[0].adb_serial == "emulator-5554"  # đang chạy → resolve
    assert instances[1].adb_serial is None  # đang tắt → không hỏi serial


# ---------------------------------------------------------------------------
# serial resolution
# ---------------------------------------------------------------------------


def test_default_serial_formula() -> None:
    assert default_serial(0) == "emulator-5554"
    assert default_serial(3) == "emulator-5560"


def test_resolve_serial_prefers_ldconsole_answer() -> None:
    mgr = make_manager({"adb": "emulator-5558\n"})
    assert mgr.resolve_serial(0) == "emulator-5558"  # tin ldconsole hơn công thức


def test_resolve_serial_falls_back_to_formula_checked_against_adb() -> None:
    mgr = make_manager({}, serials=["emulator-5556"])  # lệnh adb của ldconsole fail
    assert mgr.resolve_serial(1) == "emulator-5556"


def test_resolve_serial_returns_none_instead_of_guessing() -> None:
    mgr = make_manager({}, serials=["emulator-9999"])  # công thức không khớp adb devices
    assert mgr.resolve_serial(1) is None


def test_resolve_serial_ignores_junk_output() -> None:
    mgr = make_manager({"adb": "error: something\n"}, serials=[])
    assert mgr.resolve_serial(0) is None


# ---------------------------------------------------------------------------
# lifecycle commands
# ---------------------------------------------------------------------------


def test_start_stop_send_expected_arguments() -> None:
    runner = FakeRunner({"launch": "", "quit": ""})
    mgr = LDPlayerManager("ld.exe", runner=runner, adb_serials=list)
    mgr.start(2)
    mgr.stop(2)
    assert runner.calls == [["launch", "--index", "2"], ["quit", "--index", "2"]]


def test_set_resolution_refuses_running_instance() -> None:
    mgr = make_manager({"isrunning": "running"})
    with pytest.raises(LDConsoleError, match="stop it before"):
        mgr.set_resolution(0)


def test_set_resolution_sends_modify_when_stopped() -> None:
    runner = FakeRunner({"isrunning": "stop", "modify": ""})
    mgr = LDPlayerManager("ld.exe", runner=runner, adb_serials=list)
    mgr.set_resolution(1, 960, 540, 160)
    assert ["modify", "--index", "1", "--resolution", "960,540,160"] in runner.calls


# ---------------------------------------------------------------------------
# clone (ldconsole copy, W2 provision)
# ---------------------------------------------------------------------------


class SequencedRunner:
    """list2 trả kết quả KHÁC NHAU trước/sau lệnh copy."""

    def __init__(
        self,
        list2_before: str,
        list2_after: str,
        *,
        template_running: bool = False,
        copy_error: LDConsoleError | None = None,
    ) -> None:
        self._before = list2_before
        self._after = list2_after
        self._running = template_running
        self._copy_error = copy_error
        self._copied = False
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "copy":
            self._copied = True
            if self._copy_error is not None:
                raise self._copy_error
            return ""
        if args[0] == "list2":
            return self._after if self._copied else self._before
        if args[0] == "isrunning":
            return "running" if self._running else "stop"
        raise LDConsoleError(f"unexpected: {args}")

    @property
    def copied(self) -> bool:
        return self._copied


def clone_manager(
    tmp_path: Path, runner: SequencedRunner, *, template_bytes: int = 100, free: int = 10**12
) -> LDPlayerManager:
    """Manager whose vms dir holds a fake template disk (vms/leidian0)."""
    template = tmp_path / "vms" / "leidian0"
    template.mkdir(parents=True)
    (template / "data.vmdk").write_bytes(b"\0" * template_bytes)
    return LDPlayerManager(
        "ldconsole.exe",
        runner=runner,
        adb_serials=lambda: [],
        vms_dir=tmp_path / "vms",
        free_bytes=lambda path: free,
    )


def test_clone_returns_new_index(tmp_path: Path) -> None:
    runner = SequencedRunner(
        "0,template,1,1,0,1,1",
        "0,template,1,1,0,1,1\n3,farm05,1,1,0,1,1",
    )
    assert clone_manager(tmp_path, runner).clone("farm05", 0) == 3
    assert ["copy", "--name", "farm05", "--from", "0"] in runner.calls


def test_clone_prefers_matching_name_among_new(tmp_path: Path) -> None:
    runner = SequencedRunner(
        "0,template,1,1,0,1,1",
        "0,template,1,1,0,1,1\n3,khac,1,1,0,1,1\n4,farm05,1,1,0,1,1",
    )
    assert clone_manager(tmp_path, runner).clone("farm05", 0) == 4


def test_clone_fails_when_nothing_new(tmp_path: Path) -> None:
    runner = SequencedRunner("0,template,1,1,0,1,1", "0,template,1,1,0,1,1")
    with pytest.raises(LDConsoleError, match="cannot identify"):
        clone_manager(tmp_path, runner).clone("farm05", 0)


def test_clone_ignores_copy_exit_code_when_list2_shows_the_clone(tmp_path: Path) -> None:
    # đo thực tế 01/11 (LDPlayer 9): copy THÀNH CÔNG nhưng ldconsole trả exit 1,
    # không in gì -> đây chính là lỗi "Lỗi không xác định" khi bấm "Cấp máy mới"
    runner = SequencedRunner(
        "0,template,1,1,0,1,1",
        "0,template,1,1,0,1,1\n4,farm05,0,0,0,-1,-1",
        copy_error=LDConsoleError("ldconsole copy failed (exit 1): <no output>"),
    )
    assert clone_manager(tmp_path, runner).clone("farm05", 0) == 4


def test_clone_rejects_copy_that_never_got_its_name(tmp_path: Path) -> None:
    # trong lúc copy, LDPlayer liệt kê máy mới với tên tạm "LDPlayer-N" và chỉ
    # đổi sang tên thật khi copy XONG -> còn tên tạm = copy dở (timeout/hỏng),
    # không được gán cho acc; báo index để người xoá máy mồ côi
    runner = SequencedRunner(
        "0,template,1,1,0,1,1",
        "0,template,1,1,0,1,1\n4,LDPlayer-4,0,0,0,-1,-1",
        copy_error=LDConsoleError("ldconsole copy timed out after 60.0s"),
    )
    with pytest.raises(LDConsoleError, match=r"cannot identify.*4.*LDPlayer-4.*timed out"):
        clone_manager(tmp_path, runner).clone("farm05", 0)


def test_clone_refuses_running_template(tmp_path: Path) -> None:
    # copy đĩa của máy đang chạy cho ra bản sao không nhất quán (máy #5 ngày 01/11)
    runner = SequencedRunner("0,template,1,1,1,1,1", "", template_running=True)
    with pytest.raises(LDConsoleError, match="template instance 0 is running"):
        clone_manager(tmp_path, runner).clone("farm05", 0)
    assert not runner.copied


def test_clone_refuses_when_disk_cannot_hold_copy_plus_headroom(tmp_path: Path) -> None:
    runner = SequencedRunner("0,template,1,1,0,1,1", "")
    mgr = clone_manager(tmp_path, runner, template_bytes=1000, free=1999)
    with pytest.raises(LDConsoleError, match="not enough disk space"):
        mgr.clone("farm05", 0, headroom_bytes=1000)
    assert not runner.copied


def test_clone_refuses_missing_template_disk(tmp_path: Path) -> None:
    runner = SequencedRunner("0,template,1,1,0,1,1", "")
    mgr = clone_manager(tmp_path, runner)
    with pytest.raises(LDConsoleError, match="leidian7"):
        mgr.clone("farm05", 7)  # không đo được dung lượng -> không đoán, không copy
    assert not runner.copied


def test_clone_refuses_name_that_already_exists(tmp_path: Path) -> None:
    # lần bấm trước có thể đã tạo máy (3 máy "002" mồ côi ngày 01/11) ->
    # gán máy đó vào YAML, đừng clone thêm
    runner = SequencedRunner("0,template,1,1,0,1,1\n4,farm05,0,0,0,-1,-1", "")
    with pytest.raises(LDConsoleError, match=r"'farm05' already exists \(index 4\)"):
        clone_manager(tmp_path, runner).clone("farm05", 0)
    assert not runner.copied


# ---------------------------------------------------------------------------
# real subprocess runner (sys.executable stands in for ldconsole.exe)
# ---------------------------------------------------------------------------

# Giả lập `ldconsole adb` khi adb server chưa chạy: in serial rồi thoát ngay,
# nhưng để lại một tiến trình cháu sống lâu (như adb daemon) thừa hưởng
# stdout/stderr của nó. Dòng cuối là PID tiến trình cháu để test dọn dẹp.
_LEAKS_HANDLES_TO_GRANDCHILD = (
    "import subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],\n"
    "                     stdout=sys.stdout.fileno(), stderr=sys.stderr.fileno())\n"
    "print('emulator-5554')\n"
    "print(p.pid)\n"
)


def test_run_returns_when_command_exits_even_if_grandchild_keeps_output_open() -> None:
    mgr = LDPlayerManager(sys.executable)
    started = time.monotonic()
    out = mgr._run(["-c", _LEAKS_HANDLES_TO_GRANDCHILD])
    elapsed = time.monotonic() - started
    lines = out.split()
    try:
        assert lines[0] == "emulator-5554"
        assert elapsed < 10.0, f"_run blocked {elapsed:.1f}s on a pipe held by a grandchild"
    finally:
        with contextlib.suppress(OSError):  # cháu có thể đã tự thoát
            os.kill(int(lines[-1]), signal.SIGTERM)


def test_run_raises_on_timeout() -> None:
    mgr = LDPlayerManager(sys.executable)
    mgr.COMMAND_TIMEOUT_S = 1.0
    with pytest.raises(LDConsoleError, match="timed out"):
        mgr._run(["-c", "import time; time.sleep(30)"])


def test_run_raises_with_output_on_nonzero_exit() -> None:
    mgr = LDPlayerManager(sys.executable)
    with pytest.raises(LDConsoleError, match=r"exit 3\): no such instance"):
        mgr._run(["-c", "import sys; sys.stderr.write('no such instance'); sys.exit(3)"])
