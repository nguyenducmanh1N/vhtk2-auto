"""Acceptance runner for the sell task (R3b), per PLAN.md 4.3 DoD.

Ensures the account is logged in (LoginTask is idempotent — near-instant when
already in-game), then runs SellTask end-to-end against a real device and
prints the SellReport. With ``safety.dry_run`` ON (default) nothing is sold;
the report lists WOULD SELL items and evidence screenshots land in
``logs/dry_run/``.

Example:
    python tools/run_sell.py --account config/accounts/dev.yaml
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
from vhtk.tasks.sell import SellTask


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the sell task against a real device")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--account", required=True, help="account YAML, e.g. accounts/dev.yaml")
    parser.add_argument(
        "--skip-login", action="store_true", help="assume already in-game, run sell directly"
    )
    args = parser.parse_args()

    settings: Settings = load_settings(Path(args.settings))
    setup_logging(settings.logging.dir, settings.logging.level)
    account = load_account(Path(args.account))

    store = TemplateStore(settings.templates.root)
    device = Device(account.ldplayer.adb_serial, settings.safety, settings.ldplayer.resolution)
    vision = Vision(store, device.screenshot, settings.vision, settings.templates.match_threshold)
    notifier = NullNotifier()
    safety = Safety(settings.safety, notifier, settings.logging.dir / "dry_run")
    recovery = Recovery(
        device,
        vision,
        notifier,
        settings.logging.dir / "unknown",
        app_in_foreground=lambda: device.app_in_foreground(settings.game.package_name),
    )
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

    print(f"dry_run={settings.safety.dry_run}  confirm_mode={settings.safety.confirm_mode}")
    try:
        if not args.skip_login:
            print("== login (idempotent) ==")
            login = LoginTask(ctx).run()
            print(f"  -> {login.status.value} (final={login.final_state})")
            if login.status is not TaskStatus.SUCCESS:
                return 1

        print("== sell ==")
        task = SellTask(ctx)
        t0 = time.monotonic()
        result = task.run()
        dt = time.monotonic() - t0
        r = task.report
        print(
            f"  -> {result.status.value} in {dt:.1f}s "
            f"(final={result.final_state}, recovers={result.recover_count}"
            f"{', reason=' + result.reason if result.reason else ''})"
        )
        print(
            f"\n== report: sold={r.sold} would_sell={r.would_sell} "
            f"kept_unknown={r.skipped_unknown} kept_quality={r.skipped_quality} =="
        )
        for line in r.details:
            print(f"  {line}")
        return 0 if result.status is TaskStatus.SUCCESS else 1
    finally:
        device.close()


if __name__ == "__main__":
    sys.exit(main())
