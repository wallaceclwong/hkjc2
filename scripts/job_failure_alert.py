#!/usr/bin/env python3
"""Send Telegram alert when a systemd job fails. Called by cp-failure-notify@.service."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from notify import send_telegram_sync

failed_unit = sys.argv[1] if len(sys.argv) > 1 else "unknown-unit"
send_telegram_sync(f"*JOB FAILED*\nUnit: {failed_unit}")
