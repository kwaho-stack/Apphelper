"""텔레그램 봇으로 휴대폰 알림 보내기.

준비: 텔레그램에서 @BotFather → /newbot 으로 봇을 만들고 받은 토큰을 앱에 넣는다.
그 봇에게 아무 메시지나 한 번 보낸 뒤 '채팅 ID 찾기'를 누르면 내 채팅 ID 가 채워진다.
토큰은 이 PC 의 data/config.json 에만 저장된다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


def _call(token: str, method: str, body: dict | None = None, timeout: float = 15) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(API.format(token=token, method=method), data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            doc = json.loads(e.read() or b"{}")
        except ValueError:
            doc = {}
        raise TelegramError(doc.get("description") or f"HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise TelegramError(f"연결 실패: {e}") from None
    if not doc.get("ok"):
        raise TelegramError(doc.get("description") or "알 수 없는 오류")
    return doc


def send(token: str, chat_id: str, text: str) -> None:
    if not (token and chat_id):
        raise TelegramError("봇 토큰과 채팅 ID 를 먼저 넣으세요")
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text})


def find_chat_id(token: str) -> str:
    """봇에게 최근 메시지를 보낸 채팅의 ID. 없으면 TelegramError."""
    if not token:
        raise TelegramError("봇 토큰을 먼저 넣으세요")
    updates = _call(token, "getUpdates").get("result") or []
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            return str(chat["id"])
    raise TelegramError("봇에게 받은 메시지가 없습니다. 텔레그램에서 그 봇에게 아무 메시지나 보낸 뒤 다시 누르세요")
