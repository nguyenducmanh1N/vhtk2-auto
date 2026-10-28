"""Worker registry: which account stem is run by which OS process.

Replaces the old global ``pgrep -f run_farm.py`` (which cannot tell workers
apart and does not exist on Windows). Each worker process writes one JSON
file ``<dir>/<stem>.json`` holding its PID and account path; liveness is
verified through psutil AND the process command line must still mention the
account path — a recycled PID never passes for a live worker.

The registry is pure bookkeeping: it never starts or stops anything.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import structlog

log = structlog.get_logger(__name__)

# Trả về cmdline của PID, None nếu tiến trình không tồn tại/không đọc được
CmdlineReader: TypeAlias = Callable[[int], "list[str] | None"]


def _psutil_cmdline(pid: int) -> list[str] | None:
    import psutil

    try:
        return list(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


@dataclass(frozen=True)
class WorkerInfo:
    """One registered worker as recorded on disk."""

    stem: str
    pid: int
    account_path: str
    alive: bool


class WorkerRegistry:
    """File-based ``{stem -> PID}`` map shared by workers, GUI and CLI.

    Args:
        dir: registry directory (e.g. ``logs/workers``); created on demand.
        cmdline: PID -> command line reader override (tests); defaults to
            psutil, which works on both macOS and Windows.
    """

    def __init__(self, dir: Path, cmdline: CmdlineReader | None = None) -> None:
        self._dir = dir
        self._cmdline = cmdline or _psutil_cmdline

    def _path(self, stem: str) -> Path:
        return self._dir / f"{stem}.json"

    def register(self, stem: str, pid: int, account_path: Path) -> None:
        """Record that ``stem`` is now run by ``pid`` (overwrites a stale entry)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {"stem": stem, "pid": pid, "account": str(account_path)}
        tmp = self._path(stem).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self._path(stem))
        log.info("worker registered", stem=stem, pid=pid)

    def unregister(self, stem: str) -> None:
        """Remove the entry; missing entries are fine (idempotent)."""
        self._path(stem).unlink(missing_ok=True)

    def info(self, stem: str) -> WorkerInfo | None:
        """The recorded entry for ``stem``, liveness-checked; None if absent."""
        path = self._path(stem)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            pid = int(raw["pid"])
            account = str(raw["account"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            log.warning("unreadable registry entry", stem=stem)
            return None
        return WorkerInfo(
            stem=stem, pid=pid, account_path=account, alive=self._is_ours(pid, account)
        )

    def alive(self, stem: str) -> bool:
        """Whether a live worker process is registered for ``stem``."""
        inf = self.info(stem)
        return inf is not None and inf.alive

    def live_stems(self) -> list[str]:
        """Stems whose registered process is still alive."""
        if not self._dir.is_dir():
            return []
        return sorted(
            f.stem for f in self._dir.glob("*.json") if self.alive(f.stem)
        )

    def _is_ours(self, pid: int, account: str) -> bool:
        # PID sống thôi chưa đủ — PID bị HĐH tái cấp cho tiến trình khác thì
        # cmdline không còn nhắc tới file account của worker này nữa
        cmdline = self._cmdline(pid)
        if cmdline is None:
            return False
        joined = " ".join(cmdline)
        return account in joined or Path(account).name in joined
