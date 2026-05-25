import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

_PREFIX = "[LUNAR LEAP]\n"


def _payloads(text: str) -> list[dict]:
    body = _PREFIX + text
    return [
        {"chat_id": TELEGRAM_CHAT_ID, "text": body[:4000], "parse_mode": "Markdown"},
        {"chat_id": TELEGRAM_CHAT_ID, "text": body.replace("*", "").replace("_", "").replace("`", "")[:4096]},
    ]


def send_telegram_sync(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for payload in _payloads(text):
        try:
            r = httpx.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return True
        except Exception:
            pass
    return False


async def send_telegram_async(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        for payload in _payloads(text):
            try:
                r = await client.post(url, json=payload, timeout=10)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
    return False
