"""자동 로그인 (버튼을 누를 때만 실행).

항목마다:
  1. 이미 열린 창을 찾는다 (실행 파일 이름 + 창 제목에 들어간 글자)
  2. 없으면 지정한 경로로 연다 (웹은 브라우저로 주소를 연다) → 창이 뜰 때까지 기다린다
  3. 창을 앞으로 가져와 (설정하면 화면 오른쪽 절반 등으로 배치한 뒤) 아이디 칸 → 아이디,
     비밀번호 칸(없으면 Tab) → 비밀번호, Enter
좌표는 창 왼쪽 위 기준이라 창이 옮겨져도 된다. 비밀번호는 Windows 계정으로만 풀리게 암호화(DPAPI)해서 저장한다.
"""
from __future__ import annotations

import base64
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

KINDS = {"program": "프로그램", "web": "웹 사이트"}
# 로그인 전 창 배치 (Win+→ / Win+← 처럼). 배치한 뒤의 창 기준으로 칸 좌표를 쓰므로 창 크기가 늘 같아진다.
LAYOUTS = {"none": "그대로", "right": "오른쪽 절반 (Win+→)", "left": "왼쪽 절반 (Win+←)", "max": "최대화"}


@dataclass
class LoginTarget:
    name: str = ""
    kind: str = "program"        # program | web
    enabled: bool = True         # '전체 자동 로그인'에 포함
    exe: str = ""                # 창을 찾을 실행 파일 이름 (예: AtMessenger.exe, chrome.exe)
    title: str = ""              # 창 제목에 들어가는 글자 (비우면 검사 안 함)
    path: str = ""               # 프로그램 경로 / 웹은 브라우저 경로(비우면 기본 브라우저)
    url: str = ""                # 웹 사이트 주소
    user: str = ""
    secret: str = ""             # 보호된 비밀번호 (protect())
    id_x: int = 0                # 아이디 칸 (창 왼쪽 위 기준, 0,0 이면 클릭 안 함 = 이미 커서가 있는 칸)
    id_y: int = 0
    pw_x: int = 0                # 비밀번호 칸 (0,0 이면 아이디 뒤에 Tab)
    pw_y: int = 0
    press_enter: bool = True
    launch_wait: int = 30        # 열고 나서 창을 기다리는 최대 시간(초)
    ready_delay: float = 2.0     # 창이 뜬 뒤 로그인 화면이 준비될 때까지 기다리는 시간(초)
    layout: str = "none"         # 로그인 전 창 배치 (LAYOUTS)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LoginTarget":
        d = d or {}
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        t = cls(**known)
        if t.kind not in KINDS:
            t.kind = "program"
        if t.layout not in LAYOUTS:
            t.layout = "none"
        return t

    @property
    def password(self) -> str:
        return unprotect(self.secret)

    @password.setter
    def password(self, value: str) -> None:
        self.secret = protect(value) if value else ""


def default_targets() -> list[LoginTarget]:
    return [
        LoginTarget(name="Genian", kind="program"),
        LoginTarget(name="웹 사이트", kind="web"),
        LoginTarget(name="AT메신저", kind="program", exe="AtMessenger.exe"),
    ]


# ---------------------------------------------------------------- 비밀번호 보호

def protect(text: str) -> str:
    raw = text.encode("utf-8")
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(raw, encrypt=True)).decode("ascii")
    return "b64:" + base64.b64encode(raw).decode("ascii")  # Windows 가 아닌 곳(테스트)용


def unprotect(secret: str) -> str:
    if not secret:
        return ""
    kind, _, body = secret.partition(":")
    raw = base64.b64decode(body)
    if kind == "dpapi":
        if sys.platform != "win32":
            return ""
        try:
            return _dpapi(raw, encrypt=False).decode("utf-8")
        except OSError:
            return ""  # 다른 PC·다른 Windows 계정에서 만든 값
    return raw.decode("utf-8")


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if encrypt:
        ok = fn(ctypes.byref(src), "apphelper", None, None, None, 0x1, ctypes.byref(out))  # UI_FORBIDDEN
    else:
        ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI 실패 ({ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# ---------------------------------------------------------------- 실행

@dataclass
class Win:
    hwnd: Any
    x: int
    y: int
    iconic: bool = False


class LoginOps(Protocol):
    def find(self, t: LoginTarget) -> Win | None: ...
    def launch(self, t: LoginTarget) -> None: ...
    def focus(self, w: Win) -> Win: ...          # 앞으로 가져오고(최소화면 복원) 지금 위치를 돌려준다
    def arrange(self, w: Win, layout: str) -> Win: ...  # 창 배치 후 지금 위치
    def click(self, x: int, y: int) -> None: ...
    def select_all(self) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, name: str) -> None: ...        # "tab" | "enter"
    def sleep(self, seconds: float) -> None: ...


@dataclass
class LoginResult:
    name: str
    ok: bool
    message: str
    steps: list[str] = field(default_factory=list)

    def line(self) -> str:
        return f"{'✅' if self.ok else '❌'} {self.name}: {self.message}"


def check(t: LoginTarget) -> str | None:
    """실행 전에 빠진 설정이 있으면 설명, 없으면 None."""
    if not t.user:
        return "아이디를 입력하세요"
    if not t.secret:
        return "비밀번호를 입력하세요"
    if t.kind == "web":
        if not t.url and not t.title:
            return "웹 주소나 창 제목 글자를 입력하세요"
    elif not (t.exe or t.title):
        return "창 지정(실행 파일 이름)이나 창 제목 글자를 입력하세요"
    return None


def run_login(t: LoginTarget, ops: LoginOps, poll: float = 0.5) -> LoginResult:
    name = t.name or KINDS.get(t.kind, "항목")
    err = check(t)
    if err:
        return LoginResult(name, False, err)
    password = t.password
    if not password:
        return LoginResult(name, False, "저장된 비밀번호를 풀 수 없습니다 (다른 PC·계정에서 저장함). 다시 입력하세요")
    steps = []
    w = ops.find(t)
    if w is None:
        can_launch = t.path if t.kind == "program" else (t.url or t.path)
        if not can_launch:
            return LoginResult(name, False, "열린 창이 없고, 열 경로(주소)도 지정되지 않았습니다", steps)
        ops.launch(t)
        steps.append("열기")
        waited = 0.0
        while w is None and waited < t.launch_wait:
            ops.sleep(poll)
            waited += poll
            w = ops.find(t)
        if w is None:
            return LoginResult(name, False, f"열었지만 {t.launch_wait}초 안에 창을 찾지 못했습니다", steps)
        ops.sleep(t.ready_delay)
    else:
        steps.append("열린 창 사용")
    w = ops.focus(w)
    ops.sleep(0.3)
    if t.layout != "none":
        w = ops.arrange(w, t.layout)
        ops.sleep(0.4)
        steps.append("배치")
    if t.id_x or t.id_y:
        ops.click(w.x + t.id_x, w.y + t.id_y)
        ops.sleep(0.15)
    ops.select_all()
    ops.type_text(t.user)
    if t.pw_x or t.pw_y:
        ops.click(w.x + t.pw_x, w.y + t.pw_y)
        ops.sleep(0.15)
    else:
        ops.key("tab")
    ops.select_all()
    ops.type_text(password)
    if t.press_enter:
        ops.key("enter")
    steps.append("입력")
    return LoginResult(name, True, "로그인 입력 완료" + (" (새로 열어서)" if "열기" in steps else " (열린 창)"), steps)
