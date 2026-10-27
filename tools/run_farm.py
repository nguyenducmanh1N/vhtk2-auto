"""Acceptance runner for the farm cycle (R3), per PLAN.md 4.2 DoD.

Minimal single-account loop (the real scheduler arrives in 5b):

    [login if needed] -> FARM -> signal?
        HP_LOW  -> RETREAT -> FARM again
        other   -> log it (bag/gold handled by later phases) -> FARM again
    RELOGIN_REQUIRED from any task -> LOGIN -> continue
    ABORTED -> stop (evidence already saved by the task)
    KILLED  -> stop cleanly (kill-switch)

Example:
    python tools/run_farm.py --account config/accounts/dev.yaml --max-cycles 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Cho phép chạy trực tiếp khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhtk.config import Settings, load_account, load_settings
from vhtk.core.device import Device
from vhtk.core.fsm import BaseTask, TaskContext, TaskResult, TaskStatus
from vhtk.core.recovery import Recovery
from vhtk.core.safety import Safety
from vhtk.core.vision import TemplateStore, Vision
from vhtk.logging_setup import setup_logging
from vhtk.notify import NullNotifier
from vhtk.tasks.farm import FarmSignal, FarmTask
from vhtk.tasks.login import LoginTask
from vhtk.tasks.retreat import RetreatTask
from vhtk.tasks.sell import SellTask


def run_with_relogin(ctx: TaskContext, task: BaseTask, max_relogins: int = 2) -> TaskResult:
    """Run one task; on RELOGIN_REQUIRED, log in and retry (bounded).

    Keeps the SAME task instance across retries so attributes like
    ``FarmTask.signal`` stay readable by the caller afterwards.
    """
    result = task.run()
    relogins = 0
    while result.status is TaskStatus.RELOGIN_REQUIRED and relogins < max_relogins:
        relogins += 1
        print("  -> thrown back to login, re-logging in")
        login = LoginTask(ctx).run()
        if login.status is not TaskStatus.SUCCESS:
            return login
        result = task.run()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the farm/retreat cycle on a real device")
    parser.add_argument("--settings", default="config/settings.yaml")
    parser.add_argument("--account", required=True, help="account YAML, e.g. accounts/dev.yaml")
    parser.add_argument(
        "--max-cycles", type=int, default=0,
        help="stop after N completed farm runs (0 = run until ABORT/kill-switch)",
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

    cycles = 0
    retreats = 0
    try:
        # chạy FARM trước, không login trước: FarmTask tự xử lý màn hình bất kỳ
        # (kể cả bản đồ đang mở che HUD); nếu chưa đăng nhập, recovery sẽ báo
        # RELOGIN_REQUIRED và run_with_relogin lo phần login
        while True:
            cycles += 1
            print(f"== farm cycle {cycles} ==")
            farm = FarmTask(ctx)
            result = run_with_relogin(ctx, farm)
            if result.status is TaskStatus.KILLED:
                print("kill-switch — stopped cleanly")
                return 0
            if result.status is not TaskStatus.SUCCESS:
                print(f"farm ended: {result.status.value} ({result.reason})")
                return 1
            print(f"  farm signal: {farm.signal.value if farm.signal else 'none'}")
            if farm.signal is FarmSignal.HP_LOW:
                retreat = run_with_relogin(ctx, RetreatTask(ctx))
                if retreat.status is TaskStatus.KILLED:
                    print("kill-switch — stopped cleanly")
                    return 0
                if retreat.status is not TaskStatus.SUCCESS:
                    print(f"retreat ended: {retreat.status.value} ({retreat.reason})")
                    return 1
                retreats += 1
                # đang đứng trong thành — tranh thủ bán đồ rác mỗi N chu kỳ
                if retreats % settings.sell.every_n_retreats == 0:
                    sell = SellTask(ctx)
                    sres = run_with_relogin(ctx, sell)
                    if sres.status is TaskStatus.KILLED:
                        print("kill-switch — stopped cleanly")
                        return 0
                    r = sell.report
                    print(
                        f"  sell: {sres.status.value} — sold={r.sold}"
                        f" would_sell={r.would_sell} kept_unknown={r.skipped_unknown}"
                        f" kept_quality={r.skipped_quality}"
                    )
                    # bán lỗi không chặn vòng farm — thử lại ở chu kỳ sau
                    if sres.status is not TaskStatus.SUCCESS:
                        print(f"  sell failed ({sres.reason}), continuing farm")
            if args.max_cycles and cycles >= args.max_cycles:
                print(f"== done: {cycles} cycles ==")
                return 0
    finally:
        device.close()


if __name__ == "__main__":
    sys.exit(main())
