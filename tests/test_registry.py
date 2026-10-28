"""Tests for the worker registry (stem -> PID bookkeeping, PID-reuse safety)."""

from __future__ import annotations

from pathlib import Path

from vhtk.registry import WorkerRegistry


def make_registry(tmp_path: Path, cmdlines: dict[int, list[str] | None]) -> WorkerRegistry:
    return WorkerRegistry(tmp_path / "workers", cmdline=lambda pid: cmdlines.get(pid))


def test_register_and_alive(tmp_path: Path) -> None:
    acc = tmp_path / "accounts" / "farm01.yaml"
    reg = make_registry(tmp_path, {111: ["python", "-m", "vhtk.worker", "--account", str(acc)]})
    reg.register("farm01", 111, acc)
    info = reg.info("farm01")
    assert info is not None and info.pid == 111 and info.alive
    assert reg.alive("farm01")
    assert reg.live_stems() == ["farm01"]


def test_dead_process_not_alive(tmp_path: Path) -> None:
    reg = make_registry(tmp_path, {})  # không PID nào tồn tại
    reg.register("farm01", 222, tmp_path / "farm01.yaml")
    info = reg.info("farm01")
    assert info is not None and not info.alive
    assert not reg.alive("farm01")
    assert reg.live_stems() == []


def test_recycled_pid_not_ours(tmp_path: Path) -> None:
    # PID còn sống nhưng là tiến trình KHÁC (HĐH tái cấp PID) -> không nhận
    reg = make_registry(tmp_path, {333: ["chrome", "--headless"]})
    reg.register("farm01", 333, tmp_path / "farm01.yaml")
    assert not reg.alive("farm01")


def test_unregister_idempotent(tmp_path: Path) -> None:
    reg = make_registry(tmp_path, {})
    reg.unregister("never-registered")  # không nổ lỗi
    reg.register("farm01", 1, tmp_path / "a.yaml")
    reg.unregister("farm01")
    assert reg.info("farm01") is None


def test_corrupt_entry_returns_none(tmp_path: Path) -> None:
    reg = make_registry(tmp_path, {})
    (tmp_path / "workers").mkdir(parents=True)
    (tmp_path / "workers" / "bad.json").write_text("{not json", encoding="utf-8")
    assert reg.info("bad") is None
    assert reg.live_stems() == []


def test_matches_by_account_basename(tmp_path: Path) -> None:
    # worker spawn bằng đường dẫn tương đối, registry lưu tuyệt đối -> khớp theo tên file
    acc = tmp_path / "config" / "accounts" / "farm02.yaml"
    reg = make_registry(
        tmp_path, {444: ["python", "-m", "vhtk.worker", "--account", "config/accounts/farm02.yaml"]}
    )
    reg.register("farm02", 444, acc)
    assert reg.alive("farm02")
