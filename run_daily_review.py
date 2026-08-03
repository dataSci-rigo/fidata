#!/usr/bin/env python3
"""fiData/run_daily_review.py — quick Claude-generated daily summary, sent to
Telegram only. Meant to run once/day shortly after a run_pipeline.py refresh
(see REFACTOR_PLAN.md Phase 6 for the systemd timer schedule)."""
import json
import os
import sys

from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_pipeline import ALERTED_EARNINGS_FILE, DATA_DIR, run as run_pipeline
from ai_review import daily_summary
from telegram_alert import send_telegram

if __name__ == '__main__':
    _env = dotenv_values(os.path.join(DATA_DIR, '.env'))
    for k, v in _env.items():
        os.environ.setdefault(k, v)

    result = run_pipeline()
    summary = daily_summary(result['combined'], result['earn_cache'], result['alerts_sent'])
    send_telegram(summary)
    print(summary)
