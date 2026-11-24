"""페이지에서 보낸 원격 요청(스크린샷)을 읽고, 스크린샷을 암호화한다.

- 요청: 페이지가 GitHub API 로 `apphelper-remote` 브랜치의 request.json 을 덮어쓴다
  (부모 없는 커밋 + force, 이력 없음). 내용: {"screenshot": {"id": "...", "at": "UTC ISO"}}
- 결과 페이지(gh-pages)는 공개이므로 스크린샷은 AES-256-GCM 으로 암호화해서 올린다.
  키 = PBKDF2-HMAC-SHA256(GitHub 토큰, KEY_SALT, KEY_ITERATIONS). 토큰을 가진 사람만 풀 수 있다.
  파일 형식: nonce(12바이트) + 암호문 + 태그(16바이트). 페이지는 WebCrypto 로 같은 방식으로 푼다.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

REMOTE_BRANCH = "apphelper-remote"   # 페이지 → PC (요청)
REQUEST_FILE = "request.json"        # (예전 형식, 평문) — 읽기만 지원
REQUEST_BIN = "request.bin"          # 암호화된 요청
STATUS_BRANCH = "apphelper-status"   # PC → 페이지 (켜짐·전원 예약 상태, 60초마다)
STATUS_BIN = "status.bin"            # 암호화된 PC 상태
DATA_FILE = "data.bin"               # 암호화된 결과 전체 (gh-pages). results.json 에는 '암호화됨' 표시만
SCREEN_FILE = "screen.bin"
KEY_SALT = b"apphelper-screen-v1"
KEY_ITERATIONS = 100_000
MAX_REQUEST_AGE = timedelta(minutes=10)  # 이보다 오래된 요청은 무시 (앱을 늦게 켰을 때 엉뚱하게 찍지 않게)


@functools.lru_cache(maxsize=4)
def derive_key(token: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), KEY_SALT, KEY_ITERATIONS, 32)


def encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt(blob: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


def encrypt_json(obj: Any, key: bytes) -> bytes:
    return encrypt(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), key)


def decrypt_json(blob: bytes, key: bytes) -> Any:
    """풀 수 없거나(토큰이 다름) 형식이 이상하면 ValueError."""
    from cryptography.exceptions import InvalidTag
    try:
        return json.loads(decrypt(blob, key).decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("복호화 실패 (토큰이 다를 수 있음)") from e


# 공개 페이지의 results.json — 내용은 data.bin 에 암호화되어 있다는 표시만 담는다
def envelope(repo: str) -> dict[str, Any]:
    return {"enc": 1, "repo": repo}


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
