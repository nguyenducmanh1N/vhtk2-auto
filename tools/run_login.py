"""Acceptance runner for the login task (R1), per PLAN.md 4.1 DoD.

Force-stops the game, runs LoginTask end-to-end against a real device, and
repeats N times. Every run must finish SUCCESS with no manual help.

Example:
    python tools/run_login.py --account config/accounts/dev.yaml --runs 5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Cho phép chạy trực tiếp khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhtk.config import Settings, load_account, load_settings
from vhtk.core.device import Device
from vhtk.core.fsm import TaskContext, TaskStatus
from vhtk.core.recovery import Recovery
from vhtk.core.safety import Safety
from vhtk.core.vision import TemplateStore, Vision
from vhtk.logging_setup import setup_logging
from vhtk.notify import NullNotifier
from vhtk.tasks.login import LoginTask


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the login task against a real device")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--account", required=True, help="account YAML, e.g. accounts/dev.yaml")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--keep-app", action="store_true", help="do not force-stop between runs")
    args = parser.parse_args()

    settings: Settings = load_settings(Path(args.settings))
    setup_logging(settings.logging.dir, settings.logging.level)
    account = load_account(Path(args.account))

    store = TemplateStore(settings.templates.root)
    device = Device(account.ldplayer.adb_serial, settings.safety, settings.ldplayer.resolution)
    vision = Vision(store, device.screenshot, settings.vision, settings.templates.match_threshold)
    notifier = NullNotifier()
    safety = Safety(settings.safety, notifier, settings.logging.dir / "dry_run")
    recovery = Recovery(device, vision, notifier, settings.logging.dir / "unknown")
    ctx = TaskContext(
        device=device,
        vision=vision,
        safety=safety,
        recovery=recovery,
        notifier=notifier,
        settings=settings,
        account=account,
        log_dir=settings.logging.dir,
    )

    results: list[tuple[int, str, float]] = []
    ok = 0
    try:
        for run in range(1, args.runs + 1):
            if not args.keep_app:
                device.stop_app(settings.game.package_name)
                time.sleep(2.0)
            print(f"== run {run}/{args.runs} ==")
            t0 = time.monotonic()
            result = LoginTask(ctx).run()
            dt = time.monotonic() - t0
            ok += result.status is TaskStatus.SUCCESS
            results.append((run, result.status.value, dt))
            print(
                f"  -> {result.status.value} in {dt:.1f}s "
                f"(final={result.final_state}, recovers={result.recover_count}"
                f"{', reason=' + result.reason if result.reason else ''})"
            )
    finally:
        device.close()

    print(f"\n== summary: {ok}/{len(results)} SUCCESS ==")
    for run, status, dt in results:
        print(f"  run {run}: {status:<18} {dt:6.1f}s")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
