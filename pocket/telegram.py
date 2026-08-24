"""Telegram — the third door, and the proof that a door is a small file.

Everything a gateway does is here: take a string from somewhere, `bus.submit()`
it, put the reply back. No memory, no loop, no tools, no decisions. That is why
this is under a hundred lines and why a fourth door would be too.

One thing is not optional. A bot token addresses a bot anybody can find and
message, and behind this one sits a calendar, a memory and a tool registry. So
`POCKET_TELEGRAM_ALLOW` is an allow-list of chat ids, and with it unset the bot
starts, prints the id of whoever writes to it, and answers nobody. Pairing by
reading a number off your own terminal is a worse user experience and a much
better default than an assistant that talks to strangers.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
POLL_SECONDS = 25


def _call(token: str, method: str, **params) -> dict:
    url = API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                timeout=POLL_SECONDS + 10) as response:
        return json.loads(response.read())


def allowed_chats() -> set[str]:
    return {chat.strip() for chat in os.getenv("POCKET_TELEGRAM_ALLOW", "").split(",")
            if chat.strip()}


def run(bus, token: str = "", notify=print) -> int:
    """Long-poll until interrupted. Every failure is a `continue`: a chat gateway
    that exits because one HTTP call timed out is a chat gateway you cannot
    leave running."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        notify("no TELEGRAM_BOT_TOKEN — put one in .env (talk to @BotFather to get it)")
        return 1
    allow = allowed_chats()
    if not allow:
        notify("POCKET_TELEGRAM_ALLOW is empty, so nobody is answered yet. "
               "Message the bot and add the chat id it prints here.")
    notify(f"telegram · polling · {len(allow)} chat(s) allowed · ctrl-c to quit")

    offset = 0
    while True:
        try:
            updates = _call(token, "getUpdates", offset=offset, timeout=POLL_SECONDS)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            notify(f"telegram · poll failed, retrying: {type(exc).__name__}: {exc}")
            continue
        for update in updates.get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message") or {}
            text = (message.get("text") or "").strip()
            chat = str(message.get("chat", {}).get("id", ""))
            if not text or not chat:
                continue
            if chat not in allow:
                notify(f"telegram · ignored a message from chat {chat} "
                       f"(add it to POCKET_TELEGRAM_ALLOW to answer)")
                continue
            _answer(token, bus, chat, text, notify)


def _answer(token: str, bus, chat: str, text: str, notify) -> None:
    try:
        reply = bus.submit(text, source=f"telegram:{chat}")
    except Exception as exc:
        reply = f"Something broke on the way to an answer: {type(exc).__name__}: {exc}"
    for chunk in _chunks(reply):
        try:
            _call(token, "sendMessage", chat_id=chat, text=chunk)
        except (urllib.error.URLError, OSError) as exc:
            notify(f"telegram · could not deliver: {type(exc).__name__}: {exc}")
            return


def _chunks(text: str, limit: int = 4000) -> list[str]:
    """Telegram refuses messages over ~4096 characters, and a refused reply is
    indistinguishable from an assistant that ignored you."""
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
