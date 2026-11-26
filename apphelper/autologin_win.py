"""자동 로그인의 Windows 동작: 창 찾기·열기·앞으로 가져오기, 클릭, 글자 입력(SendInput 유니코드).

글자는 한/영 상태와 상관없이 그대로 들어가도록 유니코드 입력으로 보낸다.
관리자 권한으로 실행된 프로그램(일부 보안 프로그램)에는 Windows 가 일반 프로그램의 입력을 막으므로,
그런 경우 Apphelper 도 관리자 권한으로 실행해야 한다.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

from . import wincapture
from .core.autologin import LoginTarget, Win

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x2, 0x4
VK = {"tab": 0x09, "enter": 0x0D, "ctrl": 0x11, "a": 0x41, "alt": 0x12}
SW_RESTORE = 9


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def matches(w: dict, t: LoginTarget) -> bool:
    if t.exe and w["exe"].lower() != t.exe.lower():
        return False
    if t.title and t.title.lower() not in w["title"].lower():
        return False
    return bool(t.exe or t.title)


class WinLoginOps:
    def __init__(self, backend):
        self.backend = backend
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    # 창
    def find(self, t: LoginTarget) -> Win | None:
        found = [w for w in wincapture.list_windows() if matches(w, t)]
        if not found:
            return None
        w = min(found, key=lambda w: (w["iconic"], -w["w"] * w["h"]))  # 최소화 안 된 큰 창 우선
        return Win(w["hwnd"], w["x"], w["y"], w["iconic"])

    def launch(self, t: LoginTarget) -> None:
        if t.kind == "web":
            if t.path:
                subprocess.Popen([t.path, t.url] if t.url else [t.path])
            else:
                os.startfile(t.url)  # 기본 브라우저
        else:
            os.startfile(t.path)  # .exe·바로가기(.lnk) 모두, 관리자 권한이 필요한 프로그램은 확인 창이 뜬다

    def focus(self, w: Win) -> Win:
        if w.iconic:
            self.user32.ShowWindow(w.hwnd, SW_RESTORE)
            time.sleep(0.3)
        # 다른 프로그램을 앞으로 가져오려면 Alt 를 한 번 눌렀다 떼는 것이 가장 확실하다
        self._keys([(VK["alt"], 0), (VK["alt"], KEYEVENTF_KEYUP)])
        self.user32.SetForegroundWindow(w.hwnd)
        self.user32.BringWindowToTop(w.hwnd)
        time.sleep(0.2)
        for info in wincapture.list_windows():
            if info["hwnd"] == w.hwnd:
                return Win(w.hwnd, info["x"], info["y"], info["iconic"])
        return w

    # 입력
    def click(self, x: int, y: int) -> None:
        self.backend.click(x, y)

    def _keys(self, seq) -> None:
        arr = (INPUT * len(seq))()
        for i, (vk, flags) in enumerate(seq):
            arr[i].type = INPUT_KEYBOARD
            arr[i].u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
        self.user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))

    def select_all(self) -> None:
        self._keys([(VK["ctrl"], 0), (VK["a"], 0), (VK["a"], KEYEVENTF_KEYUP), (VK["ctrl"], KEYEVENTF_KEYUP)])
        time.sleep(0.05)

    def type_text(self, text: str) -> None:
        for ch in text:
            arr = (INPUT * 2)()
            for i, flags in enumerate((KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)):
                arr[i].type = INPUT_KEYBOARD
                arr[i].u.ki = KEYBDINPUT(0, ord(ch), flags, 0, 0)
            self.user32.SendInput(2, arr, ctypes.sizeof(INPUT))
            time.sleep(0.02)

    def key(self, name: str) -> None:
        vk = VK[name]
        self._keys([(vk, 0), (vk, KEYEVENTF_KEYUP)])
        time.sleep(0.05)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
