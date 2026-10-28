"""Acceptance runner for the farm cycle (R3) — thin wrapper over vhtk.worker.

The real single-account loop lives in ``src/vhtk/worker.py`` (AccountWorker,
PLAN.md 5b); this script only keeps the historical CLI working:

    python tools/run_farm.py --account config/accounts/dev.yaml --max-cycles 3

Prefer ``python -m vhtk.worker`` for new usage — same flags, same behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Cho phép chạy trực tiếp khi chưa `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vhtk.worker import main

if __name__ == "__main__":
    sys.exit(main())
