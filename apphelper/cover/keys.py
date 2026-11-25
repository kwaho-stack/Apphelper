"""화면 가리기 해제: 실제 키보드 입력(가상 키 코드)을 글자로 바꿔 모으고, 비밀번호가 입력되면 해제.

- 한/영 상태와 상관없이 영문 자판(US) 기준으로 읽는다 → 비밀번호는 영문·숫자·기호만 가능
- 틀린 글자가 섞여도 마지막에 비밀번호가 정확히 입력되면 해제 (Enter 필요 없음), Backspace 는 한 글자 지움
- 비밀번호 자체는 넘기지 않는다: 검은 화면 비밀번호의 검증값(무작위 salt + SHA-256)만 쓴다
- 첫 입력(키·클릭) 뒤 5초 안에 비밀번호를 못 치면 입력 비상종료 (Gate)
"""
from __future__ import annotations

import hashlib
import hmac
import os

MIN_LEN = 4
MAX_LEN = 64

VK_BACK, VK_SHIFT, VK_LSHIFT, VK_RSHIFT, VK_CAPITAL, VK_SPACE = 0x08, 0x10, 0xA0, 0xA1, 0x14, 0x20

_DIGIT_SHIFT = ")!@#$%^&*("
_OEM = {  # vk: (기본, Shift)
    0xBA: (";", ":"), 0xBB: ("=", "+"), 0xBC: (",", "<"), 0xBD: ("-", "_"), 0xBE: (".", ">"),
    0xBF: ("/", "?"), 0xC0: ("`", "~"), 0xDB: ("[", "{"), 0xDC: ("\\", "|"), 0xDD: ("]", "}"),
    0xDE: ("'", '"'),
}
_NUMPAD_OPS = {0x6A: "*", 0x6B: "+", 0x6D: "-", 0x6E: ".", 0x6F: "/"}


def key_char(vk: int, shift: bool, caps: bool) -> str | None:
    """가상 키 코드 → 글자 (영문 자판 기준). 글자가 아닌 키면 None."""
    if 0x41 <= vk <= 0x5A:
        c = chr(vk)
        return c if (shift != caps) else c.lower()
    if 0x30 <= vk <= 0x39:
        return _DIGIT_SHIFT[vk - 0x30] if shift else chr(vk)
    if 0x60 <= vk <= 0x69:
        return str(vk - 0x60)
    if vk in _NUMPAD_OPS:
        return _NUMPAD_OPS[vk]
    if vk in _OEM:
        return _OEM[vk][1 if shift else 0]
    if vk == VK_SPACE:
        return " "
    return None


def typable(password: str) -> bool:
    """영문 자판으로 칠 수 있는 비밀번호인지 (가리기 해제에 쓸 수 있는지)."""
    return all(32 <= ord(c) < 127 for c in password)


def make_verifier(password: str) -> dict[str, str]:
    """세션용 검증값 (메모리에서만, 파일에 저장하지 않음)."""
    salt = os.urandom(16)
    return {"salt": salt.hex(), "hash": hashlib.sha256(salt + password.encode("utf-8")).hexdigest(),
            "typable": typable(password)}


def check(verifier: dict[str, str], candidate: str) -> bool:
    digest = hashlib.sha256(bytes.fromhex(verifier["salt"]) + candidate.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, verifier["hash"])


class Unlocker:
    def __init__(self, verifier: dict[str, str], caps: bool = False):
        self.verifier = verifier
        self.caps = caps
        self.shift = False
        self.buf = ""

    def feed(self, vk: int, down: bool) -> bool:
        """키 하나 (눌림/뗌). 비밀번호가 완성되면 True."""
        if vk in (VK_SHIFT, VK_LSHIFT, VK_RSHIFT):
            self.shift = down
            return False
        if not down:
            return False
        if vk == VK_CAPITAL:
            self.caps = not self.caps
            return False
        if vk == VK_BACK:
            self.buf = self.buf[:-1]
            return False
        c = key_char(vk, self.shift, self.caps)
        if c is None:
            return False
        self.buf = (self.buf + c)[-MAX_LEN:]
        for n in range(MIN_LEN, len(self.buf) + 1):
            if check(self.verifier, self.buf[-n:]):
                self.buf = ""
                return True
        return False


ARM_SECONDS = 5  # 첫 입력(키 또는 클릭) 뒤 비밀번호를 칠 수 있는 시간


class Gate:
    """검은 화면 입력 처리: 첫 입력으로 5초 시계가 시작되고, 그 안에 비밀번호가 완성되면 해제, 아니면 비상종료.

    Win32 타이머는 바깥(guard.py)이 맡고, 여기서는 상태만 바꾼다. 각 메서드는 할 일을 돌려준다:
      "arm"     → 지금 5초 타이머를 시작하라
      "unlock"  → 해제하라
      None      → 할 일 없음
    """

    def __init__(self, verifier: dict[str, str], caps: bool = False):
        self.unlocker = Unlocker(verifier, caps)
        self.armed = False
        self.done: str | None = None  # "unlock" | "emergency"

    def _arm(self) -> str | None:
        if self.armed:
            return None
        self.armed = True
        return "arm"

    def key(self, vk: int, down: bool) -> list[str]:
        if self.done:
            return []
        actions = []
        if down:  # 어떤 키든 누르면 시작 (Shift 포함)
            a = self._arm()
            if a:
                actions.append(a)
        if self.unlocker.feed(vk, down):
            self.done = "unlock"
            actions.append("unlock")
        return actions

    def click(self) -> list[str]:
        if self.done:
            return []
        a = self._arm()
        return [a] if a else []

    def expire(self) -> bool:
        """5초가 지났다. 아직 안 풀렸으면 비상종료 → True."""
        if self.done:
            return False
        self.done = "emergency"
        return True
