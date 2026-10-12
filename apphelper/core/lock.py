"""편집 화면 잠금 비밀번호.

비밀번호 자체는 저장하지 않고 PBKDF2-SHA256 해시만 data/lock.json 에 둔다.
비밀번호를 잊으면 이 파일만 지우면 된다 (다른 설정과 동작은 그대로 남는다).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

ITERATIONS = 200_000


def _hash(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


class PasswordLock:
    FILE_NAME = "lock.json"

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / self.FILE_NAME

    def is_set(self) -> bool:
        return self._load() is not None

    def _load(self) -> dict | None:
        try:
            with self.path.open(encoding="utf-8") as f:
                d = json.load(f)
            bytes.fromhex(d["salt"]), bytes.fromhex(d["hash"]), int(d["iterations"])
            return d
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set(self, password: str) -> None:
        if not password:
            raise ValueError("빈 비밀번호는 쓸 수 없습니다")
        salt = os.urandom(16)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"salt": salt.hex(), "iterations": ITERATIONS,
                       "hash": _hash(password, salt, ITERATIONS).hex()}, f)
        os.replace(tmp, self.path)

    def verify(self, password: str) -> bool:
        d = self._load()
        if d is None:
            return False
        expected = bytes.fromhex(d["hash"])
        return hmac.compare_digest(_hash(password, bytes.fromhex(d["salt"]), int(d["iterations"])), expected)
