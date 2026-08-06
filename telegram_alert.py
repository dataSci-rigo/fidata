"""Send-only Telegram alert helper.

fiData has its own dedicated bot (FI_BOT_ID, @fi_data-style bot, see master
.env's "# fiData" section) rather than sharing PING_BOT_ID's forum group —
so this just DMs OWNER_CHAT_ID directly, no message_thread_id/topic needed.

This is send-only — fiData never receives/polls Telegram updates, so unlike
todo_list's bots it does NOT need to join run_bots.py's shared long-poller
(Telegram only allows one getUpdates poller per token; a plain sendMessage
call has no such restriction).

One-time setup: message the fi_bot on Telegram (e.g. /start) so it has a
chat to DM into — a bot cannot message a user who has never messaged it.
"""
import os

import requests

TOKEN = os.getenv('FI_BOT_ID', '')
CHAT_ID = os.getenv('OWNER_CHAT_ID', '').strip("'\"")
BASE_URL = f'https://api.telegram.org/bot{TOKEN}'


def send_telegram(text: str) -> bool:
    if not TOKEN or not CHAT_ID:
        print(f'[telegram disabled — no FI_BOT_ID/OWNER_CHAT_ID] {text}')
        return False
    payload = {'chat_id': CHAT_ID, 'text': text}
    try:
        resp = requests.post(f'{BASE_URL}/sendMessage', json=payload, timeout=10)
        if not resp.ok:
            print(f'telegram send failed ({resp.status_code}): {resp.text}')
        return resp.ok
    except Exception as e:
        print(f'telegram send error: {e}')
        return False
