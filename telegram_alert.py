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
MAX_LEN = 4096


def _send_one(text: str) -> bool:
    payload = {'chat_id': CHAT_ID, 'text': text}
    try:
        resp = requests.post(f'{BASE_URL}/sendMessage', json=payload, timeout=10)
        if not resp.ok:
            print(f'telegram send failed ({resp.status_code}): {resp.text}')
        return resp.ok
    except Exception as e:
        print(f'telegram send error: {e}')
        return False


def send_telegram(text: str) -> bool:
    if not TOKEN or not CHAT_ID:
        print(f'[telegram disabled — no FI_BOT_ID/OWNER_CHAT_ID] {text}')
        return False
    if len(text) <= MAX_LEN:
        return _send_one(text)

    # Batched alert callers can produce more than one message's worth in a
    # single join — split on line boundaries so an alert never gets cut
    # mid-sentence, rather than let Telegram's API 400 the whole send.
    chunks, chunk = [], ''
    for line in text.split('\n'):
        candidate = f'{chunk}\n{line}' if chunk else line
        if len(candidate) > MAX_LEN:
            if chunk:
                chunks.append(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        chunks.append(chunk)
    return all(_send_one(c) for c in chunks)
