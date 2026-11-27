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


# ---------------------------------------------------------------- GitHub 거쳐 보내기
# 회사망에서 텔레그램이 막혀 있으면, PC 는 GitHub 에 '알림 요청'(repository_dispatch)만 보내고
# GitHub Actions(.github/workflows/notify.yml)가 텔레그램으로 대신 보낸다. 저장소가 공개라서 내용(채팅 ID·글)은
# 봇 토큰에서 만든 키로 암호화한다. 봇 토큰은 저장소 Secrets 의 TELEGRAM_TOKEN 에 같은 값을 넣어 둔다.
NOTIFY_EVENT = "apphelper-notify"
_NOTIFY_SALT = b"apphelper-notify-v1"


def notify_key(bot_token: str) -> bytes:
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", bot_token.encode("utf-8"), _NOTIFY_SALT, 100_000)


def seal(bot_token: str, chat_id: str, text: str) -> str:
    """{"chat", "text"} 를 AES-256-GCM 으로 암호화한 base64 (nonce 12바이트 + 암호문 + 태그)."""
    import base64
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    body = json.dumps({"chat": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(nonce + AESGCM(notify_key(bot_token)).encrypt(nonce, body, None)).decode("ascii")


def unseal(bot_token: str, sealed: str) -> dict:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(sealed)
    return json.loads(AESGCM(notify_key(bot_token)).decrypt(raw[:12], raw[12:], None))
