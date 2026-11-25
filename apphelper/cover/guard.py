"""화면 가리기 프로세스 (`Apphelper.exe --cover`). Windows 전용, Qt 없이 Win32 만 쓴다.

- 화면 전체를 덮는 검은 창: 항상 위, 클릭 통과(매크로 클릭은 아래 프로그램으로), 활성화 안 됨
- SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE): 사람 눈에는 검은 화면, 화면 캡처(색 비교·이미지 찾기·
  메신저 감시·스크린샷)에는 원래 화면이 보인다 (Windows 10 2004 이상)
- 저수준 키보드·마우스 후크: 사람이 친 입력(주입 표시 없음)은 버리고, 프로그램이 보낸 입력은 통과
- 사람이 키를 누르거나 클릭하면 5초 시계가 시작되고, 그 안에 버린 키 입력으로 비밀번호가 완성되면 해제
  → 'UNLOCKED' 출력 후 종료. 5초 안에 못 치면 입력 비상종료(emergency.py: PC 잠금 → 창 닫기 → 메신저 끄기)
  뒤 검은 화면을 없애고 종료
- 가리는 동안 절전·화면 꺼짐을 막는다 (매크로가 돌아야 하므로)
- 가리는 동안 마우스 커서를 숨긴다 (cursor.py)
- 선택 상자 목록·메뉴·툴팁·새 창 같은 '항상 위' 팝업이 검은 창 위로 비치지 않게, 창이 나타나거나 순서가 바뀌는
  순간(WinEvent)마다 즉시 검은 창을 다시 맨 위로 올린다. 놓친 경우를 위해 50ms 마다도 올린다.

앱과의 약속:
  표준 입력 첫 줄: {"salt": ..., "hash": ...} (세션용 비밀번호 검증값). 이후 'STOP' → 해제 없이 끝냄(테스트용)
  종료 코드: 0 비밀번호로 해제 / 20 입력 비상종료 / 10 STOP / 2 검증값 없음 / 3 지원 안 되는 Windows /
            4 입력 차단 실패 / 5 창 실패
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
from .keys import ARM_SECONDS, Gate

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_DESTROY, WM_CLOSE, WM_TIMER = 0x02, 0x10, 0x113
WM_USER = 0x400
WM_ARM = WM_USER + 1          # 후크 → 창: 5초 타이머 시작
WM_EMERGENCY_DONE = WM_USER + 2
CLICK_MESSAGES = {0x201, 0x204, 0x207, 0x20B}  # 왼쪽·오른쪽·가운데·X 버튼 누름 (이동·휠은 아님)
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
TOPMOST_TIMER, ARM_TIMER = 1, 2
TOPMOST_MS = 50       # 평소 맨 위 유지 간격 (팝업은 WinEvent 로 즉시 처리, 이건 놓친 경우 대비)
FAST_TOPMOST_MS = 15  # 비상종료 중 맨 위 유지 간격
# WinEvent: 창이 나타남·순서 바뀜·앞으로 나옴·메뉴 열림 → 바로 검은 창을 다시 맨 위로
EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MENUPOPUPSTART = 0x0003, 0x0006
EVENT_OBJECT_SHOW, EVENT_OBJECT_REORDER = 0x8002, 0x8004
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002
OBJID_WINDOW = 0

LRESULT = ctypes.c_ssize_t
_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)  # Windows 가 아니어도 불러올 수는 있게
HOOKPROC = _FUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDPROC = _FUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = _FUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG,
                         wintypes.DWORD, wintypes.DWORD)


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
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.SetWinEventHook.restype = wintypes.HANDLE
    user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
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
EXIT_EMERGENCY = 20


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
    state = {"hwnd": None}
    gate = Gate(verifier, caps=bool(user32.GetKeyState(VK_CAPITAL) & 1))

    def emergency_worker(hwnd):
        from . import emergency
        emergency.run()
        user32.PostMessageW(hwnd, WM_EMERGENCY_DONE, 0, 0)

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_TIMER and wparam == ARM_TIMER:  # 첫 입력 뒤 5초 안에 비밀번호를 못 침
            user32.KillTimer(hwnd, ARM_TIMER)
            if gate.expire():
                # 잠금 화면이 뜰 때까지 다른 창이 위로 비치지 않게 아주 자주 맨 위로 올린다
                user32.SetTimer(hwnd, TOPMOST_TIMER, FAST_TOPMOST_MS, None)
                # 오래 걸리는 일은 다른 스레드에서 (이 스레드가 막히면 입력 후크가 풀릴 수 있다)
                threading.Thread(target=emergency_worker, args=(hwnd,), daemon=True).start()
            return 0
        if msg == WM_TIMER:  # 다른 '항상 위' 창이 올라와도 다시 맨 위로
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            return 0
        if msg == WM_ARM:
            user32.SetTimer(hwnd, ARM_TIMER, ARM_SECONDS * 1000, None)
            return 0
        if msg == WM_EMERGENCY_DONE:  # 창 닫기·메신저 끄기·잠금까지 끝났으면 검은 화면을 없앤다
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def act(actions):
        for a in actions:
            if a == "arm":
                user32.PostMessageW(state["hwnd"], WM_ARM, 0, 0)
            elif a == "unlock":
                user32.KillTimer(state["hwnd"], ARM_TIMER)
                user32.PostMessageW(state["hwnd"], WM_CLOSE, 0, 0)

    def kb_hook(code, wparam, lparam):
        if code >= 0:
            ks = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if ks.flags & LLKHF_INJECTED:
                return user32.CallNextHookEx(None, code, wparam, lparam)  # 프로그램이 보낸 입력은 통과
            down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                act(gate.key(ks.vkCode, down))
            return 1  # 사람이 친 키는 어디에도 전달하지 않는다
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def mouse_hook(code, wparam, lparam):
        if code >= 0:
            ms = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if not ms.flags & LLMHF_INJECTED:
                if wparam in CLICK_MESSAGES:  # 클릭만 5초 시계를 시작한다 (이동·휠은 무시)
                    act(gate.click())
                return 1  # 사람의 마우스 이동·클릭·휠은 버린다
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def raise_cover():
        user32.SetWindowPos(state["hwnd"], HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def win_event(_hook, event, hwnd, id_object, _child, _thread, _time):
        # 다른 프로그램 창(팝업 포함)이 나타나거나 앞으로 나오면 그려지기 전에 다시 덮는다
        if state["hwnd"] and hwnd != state["hwnd"] and (
                id_object == OBJID_WINDOW or event == EVENT_SYSTEM_MENUPOPUPSTART):
            raise_cover()

    wndproc_cb, kb_cb, mouse_cb = WNDPROC(wndproc), HOOKPROC(kb_hook), HOOKPROC(mouse_hook)  # 참조 유지
    event_cb = WINEVENTPROC(win_event)

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
    user32.SetTimer(hwnd, TOPMOST_TIMER, TOPMOST_MS, None)
    flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS  # 자기 창 이동으로 다시 불리지 않게
    event_hooks = [h for h in (
        user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MENUPOPUPSTART, None, event_cb, 0, 0, flags),
        user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_REORDER, None, event_cb, 0, 0, flags),
    ) if h]  # 실패해도 타이머로는 계속 덮는다

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
        for h in event_hooks:
            user32.UnhookWinEvent(h)
        user32.UnhookWindowsHookEx(kb)
        user32.UnhookWindowsHookEx(mouse)
        cursor.restore()
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    if gate.done == "unlock":
        say("UNLOCKED")
        return EXIT_UNLOCKED
    if gate.done == "emergency":
        say("EMERGENCY")
        return EXIT_EMERGENCY
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
