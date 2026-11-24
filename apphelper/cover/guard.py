"""화면 가리기 프로세스 (`Apphelper.exe --cover`). Windows 전용, Qt 없이 Win32 만 쓴다.

- 화면 전체를 덮는 검은 창: 항상 위, 클릭 통과(매크로 클릭은 아래 프로그램으로), 활성화 안 됨
- SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE): 사람 눈에는 검은 화면, 화면 캡처(색 비교·이미지 찾기·
  메신저 감시·스크린샷)에는 원래 화면이 보인다 (Windows 10 2004 이상)
- 저수준 키보드·마우스 후크: 사람이 친 입력(주입 표시 없음)은 버리고, 프로그램이 보낸 입력은 통과
- 버린 키 입력 중 비밀번호가 완성되면 해제 → 'UNLOCKED' 출력 후 종료
- 가리는 동안 절전·화면 꺼짐을 막는다 (매크로가 돌아야 하므로)
- 가리는 동안 마우스 커서를 숨긴다 (cursor.py)

앱과의 약속:
  표준 입력 첫 줄: {"salt": ..., "hash": ...} (세션용 비밀번호 검증값). 이후 'STOP' → 해제 없이 끝냄(테스트용)
  종료 코드: 0 비밀번호로 해제 / 10 STOP / 2 검증값 없음 / 3 지원 안 되는 Windows / 4 입력 차단 실패 / 5 창 실패
  (창 없는 exe 는 표준 출력이 없을 수 있어 결과는 종료 코드로 알린다. 표준 출력이 있으면 READY 등도 쓴다)
이 프로세스가 비정상 종료되면 Windows 가 후크를 자동으로 풀어 입력은 다시 된다.
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
from ctypes import wintypes

from . import cursor
from .keys import Unlocker

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_DESTROY, WM_CLOSE, WM_TIMER = 0x02, 0x10, 0x113
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x08, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
LWA_ALPHA = 0x2
WDA_EXCLUDEFROMCAPTURE = 0x11
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
HWND_TOPMOST = -1
SW_SHOWNOACTIVATE = 4
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x1, 0x2
VK_CAPITAL = 0x14
BLACK_BRUSH = 4
TOPMOST_TIMER = 1

LRESULT = ctypes.c_ssize_t
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)  # Windows 가 아니어도 불러올 수는 있게
HOOKPROC = _FUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = _FUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


def _api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.UINT]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.GetKeyState.restype = ctypes.c_short
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    gdi32.GetStockObject.restype = wintypes.HGDIOBJ
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    return user32, kernel32, gdi32


EXIT_UNLOCKED, EXIT_STOPPED, EXIT_BAD_INPUT, EXIT_UNSUPPORTED, EXIT_HOOK, EXIT_WINDOW = 0, 10, 2, 3, 4, 5


def say(msg: str) -> None:
    if sys.stdout is None:
        return
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def run(verifier: dict) -> int:
    user32, kernel32, gdi32 = _api()
    try:  # 화면 배율과 상관없이 실제 화면 전체를 덮도록
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()

    hinst = kernel32.GetModuleHandleW(None)
    state = {"hwnd": None, "unlocked": False}
    unlocker = Unlocker(verifier, caps=bool(user32.GetKeyState(VK_CAPITAL) & 1))

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:  # 다른 '항상 위' 창이 올라와도 다시 맨 위로
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def kb_hook(code, wparam, lparam):
        if code >= 0:
            ks = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if ks.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(None, code, wparam, lparam)  # 프로그램이 보낸 입력은 통과
            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP) and not state["unlocked"]:
                if unlocker.feed(ks.vkCode, down):
                    state["unlocked"] = True
                    user32.PostMessageW(state["hwnd"], WM_CLOSE, 0, 0)
            return 1  # 사람이 친 키는 어디에도 전달하지 않는다
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def mouse_hook(code, wparam, lparam):
        if code >= 0:
            ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if not ms.flags & LLMHF_INJECTED:
                return 1  # 사람의 마우스 이동·클릭·휠은 버린다
        return user32.CallNextHookEx(None, code, wparam, lparam)

    wndproc_cb, kb_cb, mouse_cb = WNDPROC(wndproc), HOOKPROC(kb_hook), HOOKPROC(mouse_hook)  # 참조 유지

    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc_cb
    wc.hInstance = hinst
    wc.hbrBackground = gdi32.GetStockObject(BLACK_BRUSH)
    wc.lpszClassName = "ApphelperCover"
    user32.RegisterClassW(ctypes.byref(wc))
    x, y = user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w, h = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN), user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    ex = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    hwnd = user32.CreateWindowExW(ex, "ApphelperCover", "Apphelper", WS_POPUP, x, y, w, h, None, None, hinst, None)
    if not hwnd:
        say(f"ERROR 창을 만들 수 없습니다 ({ctypes.get_last_error()})")
        return EXIT_WINDOW
    state["hwnd"] = hwnd
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
    if not user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        user32.DestroyWindow(hwnd)
        say("ERROR unsupported 이 Windows 버전에서는 화면 가리기를 쓸 수 없습니다 (Windows 10 2004 이상 필요)")
        return EXIT_UNSUPPORTED

    kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kb_cb, hinst, 0)
    mouse = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_cb, hinst, 0)
    if not kb or not mouse:
        for hook in (kb, mouse):
            if hook:
                user32.UnhookWindowsHookEx(hook)
        user32.DestroyWindow(hwnd)
        say(f"ERROR 입력을 막을 수 없습니다 ({ctypes.get_last_error()})")
        return EXIT_HOOK

    kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    cursor.hide()  # 검은 화면 위에 마우스 커서가 보이지 않게
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
    user32.SetTimer(hwnd, TOPMOST_TIMER, 500, None)

    def watch_stdin():
        for line in stdin:
            if line.strip() == "STOP":
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return
    threading.Thread(target=watch_stdin, daemon=True).start()
    say("READY")

    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnhookWindowsHookEx(kb)
        user32.UnhookWindowsHookEx(mouse)
        cursor.restore()
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    if state["unlocked"]:
        say("UNLOCKED")
        return EXIT_UNLOCKED
    return EXIT_STOPPED


def _stdin():
    if sys.stdin is not None:
        return sys.stdin
    import os
    return os.fdopen(0, "r", encoding="utf-8")  # 창 없는 exe: 부모가 넘긴 파이프


def main() -> int:
    global stdin
    if sys.platform != "win32":
        say("ERROR unsupported Windows 전용입니다")
        return EXIT_UNSUPPORTED
    try:
        stdin = _stdin()
        verifier = json.loads(stdin.readline())
        bytes.fromhex(verifier["salt"]), bytes.fromhex(verifier["hash"])
    except (OSError, ValueError, KeyError, TypeError):
        say("ERROR 비밀번호 검증값이 없습니다")
        return EXIT_BAD_INPUT
    return run(verifier)


stdin = None
