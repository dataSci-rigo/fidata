"""Send-only Telegram alert helper.

Mirrors todo_list/accountability_bot.py's send() pattern exactly: a direct
`requests.post` to the Bot API using the shared PING_BOT_ID token and
PINGER_CHANNEL_ID forum group, with a dedicated topic (FIDATA_THREAD_ID).

This is send-only — fiData never receives/polls Telegram updates, so unlike
todo_list's bots it does NOT need to join run_bots.py's shared long-poller
(Telegram only allows one getUpdates poller per token; a plain sendMessage
call has no such restriction).
"""
import os

import requests

TOKEN = os.getenv('PING_BOT_ID', '')
CHANNEL_ID = int(os.getenv('PINGER_CHANNEL_ID', '0') or '0')
THREAD_ID = int(os.getenv('FIDATA_THREAD_ID', '0') or '0')
BASE_URL = f'https://api.telegram.org/bot{TOKEN}'


def send_telegram(text: str) -> bool:
    if not TOKEN or not CHANNEL_ID:
        print(f'[telegram disabled — no PING_BOT_ID/PINGER_CHANNEL_ID] {text}')
        return False
    payload = {'chat_id': CHANNEL_ID, 'text': text}
    if THREAD_ID:
        payload['message_thread_id'] = THREAD_ID
    try:
        resp = requests.post(f'{BASE_URL}/sendMessage', json=payload, timeout=10)
        if not resp.ok:
            print(f'telegram send failed ({resp.status_code}): {resp.text}')
        return resp.ok
    except Exception as e:
        print(f'telegram send error: {e}')
        return False
