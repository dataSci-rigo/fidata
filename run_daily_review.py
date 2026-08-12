#!/usr/bin/env python3
"""fiData/run_daily_review.py — quick Claude-generated daily summary, sent to
Telegram only. Meant to run once/day shortly after a run_pipeline.py refresh
(see REFACTOR_PLAN.md Phase 6 for the systemd timer schedule)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# run_pipeline loads .env (into os.environ) before its own imports, ahead of
# anything here — importing it first means ai_review/telegram_alert below
# see FI_BOT_ID/OWNER_CHAT_ID/ANTHROPIC_API_KEY correctly even though they
# also read os.environ at their own import time. Don't reorder these imports.
from run_pipeline import ALERTED_EARNINGS_FILE, DATA_DIR, run as run_pipeline
from ai_review import daily_summary
from telegram_alert import send_telegram

if __name__ == '__main__':
    result = run_pipeline()
    summary = daily_summary(result['combined'], result['earn_cache'], result['alerts_sent'])
    send_telegram(summary)
    print(summary)
