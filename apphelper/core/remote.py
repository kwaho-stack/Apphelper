"""페이지에서 보낸 원격 요청(스크린샷)을 읽고, 스크린샷을 암호화한다.

- 요청: 페이지가 GitHub API 로 `apphelper-remote` 브랜치의 request.json 을 덮어쓴다
  (부모 없는 커밋 + force, 이력 없음). 내용: {"screenshot": {"id": "...", "at": "UTC ISO"}}
- 결과 페이지(gh-pages)는 공개이므로 스크린샷은 AES-256-GCM 으로 암호화해서 올린다.
  키 = PBKDF2-HMAC-SHA256(GitHub 토큰, KEY_SALT, KEY_ITERATIONS). 토큰을 가진 사람만 풀 수 있다.
  파일 형식: nonce(12바이트) + 암호문 + 태그(16바이트). 페이지는 WebCrypto 로 같은 방식으로 푼다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

REMOTE_BRANCH = "apphelper-remote"
REQUEST_FILE = "request.json"
SCREEN_FILE = "screen.bin"
KEY_SALT = b"apphelper-screen-v1"
KEY_ITERATIONS = 100_000
MAX_REQUEST_AGE = timedelta(minutes=10)  # 이보다 오래된 요청은 무시 (앱을 늦게 켰을 때 엉뚱하게 찍지 않게)


def derive_key(token: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), KEY_SALT, KEY_ITERATIONS, 32)


def encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt(blob: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


@dataclass(frozen=True)
class ScreenshotRequest:
    id: str
    at: datetime  # UTC

    def is_fresh(self, now_utc: datetime) -> bool:
        return now_utc - self.at <= MAX_REQUEST_AGE


def parse_request(raw: dict[str, Any] | None) -> ScreenshotRequest | None:
    """request.json 내용에서 스크린샷 요청을 꺼낸다. 형식이 이상하면 None."""
    try:
        req = (raw or {})["screenshot"]
        rid = str(req["id"])
        at = datetime.fromisoformat(str(req["at"]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if not rid or len(rid) > 64:
            return None
        return ScreenshotRequest(rid, at.astimezone(timezone.utc))
    except (KeyError, TypeError, ValueError):
        return None


def decode_contents(info: dict[str, Any]) -> dict[str, Any] | None:
    """GitHub contents API 응답(base64)을 JSON 으로."""
    content = base64.b64decode(info.get("content", "") or "").decode("utf-8")
    return json.loads(content) if content else None
