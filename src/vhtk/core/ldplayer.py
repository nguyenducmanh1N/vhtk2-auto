"""ldconsole.exe wrapper: manage LDPlayer instances from the command line.

Serial mapping rule: every Android emulator claims a (console, adb) port pair
``(5554 + 2*index, 5555 + 2*index)`` and ADB names the device after the
console port, so instance ``i`` is ``emulator-{5554 + 2*i}`` by default. The
formula breaks when another emulator already holds the port (Android Studio
AVD, Nox...) or the instance exposes ADB over plain TCP. ``resolve_serial``
therefore asks ldconsole itself first (``adb --index i get-serialno``) and
only falls back to the formula cross-checked against ``adb devices`` — an
unverifiable mapping yields ``None``, never a guess (tapping into the wrong
instance with real accounts is the worst possible failure).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TypeAlias

import structlog

from vhtk.core.errors import LDConsoleError

log = structlog.get_logger(__name__)

Runner: TypeAlias = Callable[[list[str]], str]
SerialLister: TypeAlias = Callable[[], list[str]]
FreeSpace: TypeAlias = Callable[[Path], int]

_BASE_CONSOLE_PORT = 5554
_SERIAL_RE = re.compile(r"emulator-\d+|[\d.]+:\d+")
# list2 CSV: index,name,top_hwnd,bind_hwnd,android_started,pid,vbox_pid[,w,h,dpi]
_LIST2_MIN_FIELDS = 7


@dataclass(frozen=True)
class Instance:
    """One LDPlayer instance as reported by ``ldconsole list2``."""

    index: int
    name: str
    running: bool
    adb_serial: str | None


def default_serial(index: int) -> str:
    """Serial predicted by the ``5554 + 2*index`` port convention."""
    return f"emulator-{_BASE_CONSOLE_PORT + 2 * index}"


def parse_list2_line(line: str) -> tuple[int, str, bool] | None:
    """Parse one ``list2`` CSV line into ``(index, name, android_started)``.

    Instance names containing commas are re-joined for the standard 7-column
    layout; a line that cannot be parsed returns ``None`` (caller logs it).
    Avoid commas in instance names — the format has no quoting.
    """
    fields = line.split(",")
    if len(fields) < _LIST2_MIN_FIELDS:
        return None
    try:
        index = int(fields[0])
    except ValueError:
        return None
    extra = 3 if len(fields) >= _LIST2_MIN_FIELDS + 3 else 0  # biến thể có w,h,dpi ở cuối
    tail_len = 5 + extra  # top_hwnd, bind_hwnd, android_started, pid, vbox_pid [, w, h, dpi]
    name = ",".join(fields[1 : len(fields) - tail_len])
    started_field = fields[len(fields) - tail_len + 2]
    if not name or started_field not in ("0", "1"):
        return None
    return index, name, started_field == "1"


def _list_adb_serials() -> list[str]:
    try:
        import adbutils

        return [d.serial for d in adbutils.adb.device_list()]
    except Exception as exc:  # adb server không chạy → coi như không có thiết bị
        log.warning("cannot list adb devices", error=str(exc))
        return []


class LDPlayerManager:
    """Start/stop/inspect LDPlayer instances via ``ldconsole.exe``.

    Args:
        ldconsole_path: full path to ``ldconsole.exe``.
        runner: command runner override (tests); receives ldconsole arguments
            and returns stdout.
        adb_serials: callable listing currently visible ADB serials (tests).
        vms_dir: folder holding the instances' disks (``leidian<i>``);
            defaults to ``vms`` next to ``ldconsole.exe``.
        free_bytes: free space of the disk holding a path (tests).
        copy_timeout_s: time limit of ``ldconsole copy``, which copies the
            whole template disk (~9GB) before returning — far longer than
            the other commands.
    """

    COMMAND_TIMEOUT_S = 60.0

    def __init__(
        self,
        ldconsole_path: Path | str,
        *,
        runner: Runner | None = None,
        adb_serials: SerialLister | None = None,
        vms_dir: Path | None = None,
        free_bytes: FreeSpace | None = None,
        copy_timeout_s: float = 600.0,
    ) -> None:
        self._path = Path(ldconsole_path)
        self._runner = runner or self._run
        self._adb_serials = adb_serials or _list_adb_serials
        self._vms_dir = vms_dir or self._path.parent / "vms"
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._copy_timeout_s = copy_timeout_s

    # -- queries ---------------------------------------------------------------

    def list_instances(self) -> list[Instance]:
        """All configured instances; serial resolved only for running ones."""
        out = self._runner(["list2"])
        instances: list[Instance] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = parse_list2_line(line)
            if parsed is None:
                log.warning("unparsable list2 line", line=line)
                continue
            index, name, running = parsed
            serial = self.resolve_serial(index) if running else None
            instances.append(Instance(index=index, name=name, running=running, adb_serial=serial))
        return instances

    def is_running(self, index: int) -> bool:
        """Whether the instance's Android has started."""
        out = self._runner(["isrunning", "--index", str(index)])
        return "running" in out.lower()

    def resolve_serial(self, index: int) -> str | None:
        """ADB serial of a running instance; ``None`` when unverifiable."""
        # Nguồn đáng tin nhất: hỏi thẳng ldconsole — nó tự định tuyến adb
        # tới đúng instance nên không phụ thuộc quy ước cổng.
        try:
            out = self._runner(["adb", "--index", str(index), "--command", "get-serialno"])
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if lines and _SERIAL_RE.fullmatch(lines[-1]):
                return lines[-1]
        except LDConsoleError as exc:
            log.debug("ldconsole get-serialno failed", index=index, error=str(exc))
        # Dự phòng: công thức 5554 + 2*index, đối chiếu với `adb devices`.
        candidate = default_serial(index)
        if candidate in self._adb_serials():
            return candidate
        log.warning(
            "cannot resolve adb serial, refusing to guess",
            index=index,
            candidate=candidate,
        )
        return None

    # -- lifecycle ---------------------------------------------------------------

    def start(self, index: int) -> None:
        """Launch an instance (returns immediately; boot takes a while)."""
        self._runner(["launch", "--index", str(index)])
        log.info("instance launch requested", index=index)

    def stop(self, index: int) -> None:
        """Quit an instance."""
        self._runner(["quit", "--index", str(index)])
        log.info("instance quit requested", index=index)

    def clone(self, name: str, from_index: int, *, headroom_bytes: int = 0) -> int:
        """Duplicate an instance (``ldconsole copy``) and return the new index.

        ``list2`` is the only trustworthy outcome: ``copy`` prints nothing and
        its exit code means nothing (1 on success, 0 on "player don't exist!"
        — measured 01/11). While copying, LDPlayer lists the new instance
        under a placeholder name (``LDPlayer-N``) and renames it to ``name``
        only once the copy is complete, so exactly one NEW row carrying
        ``name`` is the proof of a finished clone.

        Args:
            name: instance name, also the proof-of-completion marker.
            from_index: template instance; must be stopped.
            headroom_bytes: disk space that must remain free after the copy.

        Raises:
            LDConsoleError: template running or missing, not enough disk
                space, ``name`` already taken, or the finished clone cannot
                be identified (leftover new instances are listed so a human
                can remove them).
        """
        if self.is_running(from_index):
            raise LDConsoleError(
                f"template instance {from_index} is running; stop it before cloning "
                "(a running instance's disk copies inconsistently)"
            )
        template = self._vms_dir / f"leidian{from_index}"
        if not template.is_dir():
            raise LDConsoleError(f"template disk not found at {template}; cannot size the clone")
        needed = _dir_size(template) + headroom_bytes
        free = self._free_bytes(self._vms_dir)
        if free < needed:
            raise LDConsoleError(
                f"not enough disk space to clone instance {from_index}: need "
                f"{needed / 2**30:.1f} GB (copy + headroom), {free / 2**30:.1f} GB free"
            )
        existing = self.list_instances()
        for inst in existing:
            if inst.name == name:
                raise LDConsoleError(
                    f"an instance named {name!r} already exists (index {inst.index}); "
                    f"set ldplayer.index: {inst.index} in the account YAML or remove it "
                    "in LDPlayer instead of cloning again"
                )
        before = {inst.index for inst in existing}
        copy_error: LDConsoleError | None = None
        try:
            self._runner(["copy", "--name", name, "--from", str(from_index)])
        except LDConsoleError as exc:
            copy_error = exc  # mã thoát vô nghĩa — list2 bên dưới mới là kết luận
        new = [inst for inst in self.list_instances() if inst.index not in before]
        named = [inst for inst in new if inst.name == name]
        if len(named) != 1:
            leftovers = ", ".join(f"#{inst.index} {inst.name!r}" for inst in new) or "none"
            raise LDConsoleError(
                f"cannot identify the instance cloned from {from_index} as {name!r} "
                f"(new instances: {leftovers}; copy: {copy_error or 'ok'}) — "
                "remove leftover instances in LDPlayer before retrying"
            )
        log.info(
            "instance cloned",
            name=name,
            from_index=from_index,
            index=named[0].index,
            copy_exit=str(copy_error) if copy_error else "ok",
        )
        return named[0].index

    def set_resolution(
        self, index: int, width: int = 960, height: int = 540, dpi: int = 160
    ) -> None:
        """Standardize an instance's resolution (instance must be stopped).

        Raises:
            LDConsoleError: if the instance is running — LDPlayer only applies
                ``modify`` to stopped instances.
        """
        if self.is_running(index):
            raise LDConsoleError(f"instance {index} is running; stop it before changing resolution")
        self._runner(["modify", "--index", str(index), "--resolution", f"{width},{height},{dpi}"])
        log.info("resolution set", index=index, resolution=f"{width}x{height}@{dpi}")

    # -- internals ---------------------------------------------------------------

    def _run(self, args: list[str]) -> str:
        # Output đi qua file tạm, KHÔNG qua pipe: khi adb server chưa chạy,
        # `ldconsole adb` bật adb daemon và daemon thừa hưởng handle output —
        # đọc pipe sẽ chờ EOF tới khi daemon chết (treo vô hạn, timeout cũng
        # không cứu được). Chờ tiến trình thoát rồi đọc file thì không bị.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(
                    [str(self._path), *args], stdin=subprocess.DEVNULL, stdout=out, stderr=err
                )
            except FileNotFoundError as exc:
                raise LDConsoleError(f"ldconsole not found at {self._path}") from exc
            timeout = self._copy_timeout_s if args[0] == "copy" else self.COMMAND_TIMEOUT_S
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                proc.kill()
                proc.wait()
                raise LDConsoleError(
                    f"ldconsole {args[0]} timed out after {exc.timeout}s"
                ) from exc
            stdout, stderr = (_read_text(f) for f in (out, err))
        if returncode != 0:
            detail = stderr.strip() or stdout.strip() or "<no output>"
            raise LDConsoleError(f"ldconsole {args[0]} failed (exit {returncode}): {detail}")
        return stdout


def _dir_size(folder: Path) -> int:
    """Total bytes of the files under ``folder``."""
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def _read_text(f: IO[bytes]) -> str:
    """Decode a captured output file the way ``text=True`` would."""
    f.seek(0)
    return f.read().decode("utf-8", errors="replace").replace("\r\n", "\n")
