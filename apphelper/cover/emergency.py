"""입력 비상종료 동작 (Windows 전용, 가리기 프로세스에서 쓴다).

검은 화면에서 키를 누르거나 클릭한 뒤 5초 안에 비밀번호를 치지 못하면 차례로:
  1. 화면에 떠 있는 프로그램 창을 모두 닫는다 (창의 X 를 누른 것과 같은 WM_CLOSE)
  2. AtMessenger 프로세스를 강제로 끝낸다 (작업 관리자의 '작업 끝내기')
  3. PC 를 잠근다 (Win+L 과 같은 LockWorkStation)
그 다음 가리기 프로세스가 검은 화면을 없앤다.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

# 작업 관리자에 'AtMessenger(32비트)' 로 보이는 프로그램. 실행 파일 이름 앞부분으로 찾는다 (대소문자 무시).
KILL_PREFIXES = ("atmessenger",)

# 닫으면 안 되는 Windows 자체 창 (바탕 화면, 작업 표시줄 등)
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "ApphelperCover",
                 "Windows.UI.Core.CoreWindow", "ApplicationFrameWindow_Host"}

WM_CLOSE = 0x10
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
GW_OWNER = 4
DWMWA_CLOAKED = 14
TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x1
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * MAX_PATH)]


def should_kill(exe_name: str) -> bool:
    name = exe_name.lower()
    return any(name.startswith(p) for p in KILL_PREFIXES)


def should_close(cls: str, title: str, visible: bool, owned: bool, tool: bool, cloaked: bool,
                 pid: int, keep_pids: set[int]) -> bool:
    """작업 표시줄에 보이는 일반 프로그램 창만 닫는다."""
    return (visible and not owned and not tool and not cloaked and bool(title)
            and cls not in SHELL_CLASSES and pid not in keep_pids)


def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return user32


def close_windows(keep_pids: set[int]) -> int:
    """보이는 프로그램 창에 모두 '닫기'를 보낸다. 보낸 창 수."""
    user32 = _user32()
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:
        dwmapi = None
    targets = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cloaked = wintypes.DWORD()
        if dwmapi is not None:
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if should_close(cls.value, title.value, bool(user32.IsWindowVisible(hwnd)),
                        bool(user32.GetWindow(hwnd, GW_OWNER)),
                        bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW),
                        bool(cloaked.value), pid.value, keep_pids):
            targets.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(visit), 0)
    for hwnd in targets:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return len(targets)


def kill_messenger() -> int:
    """AtMessenger 프로세스를 강제로 끝낸다. 끝낸 수."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return 0
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if should_kill(entry.szExeFile):
                pids.append(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    killed = 0
    for pid in pids:
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            killed += bool(kernel32.TerminateProcess(h, 1))
            kernel32.CloseHandle(h)
    return killed


def lock_pc() -> bool:
    return bool(ctypes.WinDLL("user32").LockWorkStation())


def run(close_wait: float = 1.5) -> None:
    """비상종료 순서대로 실행. 하나가 실패해도 다음 단계는 한다."""
    keep = {os.getpid(), os.getppid()}  # 가리기 프로세스와 Apphelper 앱은 닫지 않는다
    for step in (lambda: close_windows(keep), lambda: time.sleep(close_wait), kill_messenger, lock_pc):
        try:
            step()
        except Exception:  # noqa: BLE001 - 어떤 단계가 실패해도 잠금까지는 간다
            pass
