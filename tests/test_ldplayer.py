"""Tests for ldconsole output parsing and serial resolution (no ldconsole needed)."""

from __future__ import annotations

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
