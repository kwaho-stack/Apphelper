"""검은 화면(화면 가리기) 전용 비밀번호. 편집 화면 비밀번호와 따로 정한다.

비밀번호 자체는 저장하지 않고 검증값(무작위 salt + SHA-256)만 data/cover.json 에 둔다.
가리기 프로세스는 키를 하나 칠 때마다 최근 입력의 끝부분 여러 개를 확인해야 해서 느린 해시(PBKDF2)는 쓸 수 없다.
잊어버리면 이 파일만 지우고 앱에서 다시 정하면 된다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .keys import MAX_LEN, MIN_LEN, make_verifier, typable


def problem(password: str) -> str | None:
    """검은 화면 비밀번호로 쓸 수 없으면 이유, 쓸 수 있으면 None."""
    if len(password) < MIN_LEN:
        return f"{MIN_LEN}자 이상 입력하세요."
    if len(password) > MAX_LEN:
        return f"{MAX_LEN}자 이하로 입력하세요."
    if not typable(password):
        return "영문·숫자·기호만 쓸 수 있습니다 (한글 X). 검은 화면에서는 한/영 상태와 상관없이 영문 자판으로 읽습니다."
    return None


class CoverPassword:
    FILE_NAME = "cover.json"

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / self.FILE_NAME

    def verifier(self) -> dict | None:
        try:
            with self.path.open(encoding="utf-8") as f:
                d = json.load(f)
            bytes.fromhex(d["salt"]), bytes.fromhex(d["hash"])
            return {"salt": d["salt"], "hash": d["hash"], "typable": True}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def is_set(self) -> bool:
        return self.verifier() is not None

    def set(self, password: str) -> None:
        err = problem(password)
        if err:
            raise ValueError(err)
        v = make_verifier(password)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"salt": v["salt"], "hash": v["hash"]}, f)
        os.replace(tmp, self.path)
