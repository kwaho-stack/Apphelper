"""입력 비상종료 동작 (Windows 전용, 가리기 프로세스에서 쓴다).

검은 화면에서 키를 누르거나 클릭한 뒤 5초 안에 비밀번호를 치지 못하면 차례로:
  1. PC 를 잠그고 (Win+L 과 같은 LockWorkStation) 잠금 화면이 실제로 뜰 때까지 기다린다
  2. 화면에 떠 있는 프로그램 창을 모두 닫는다 (창의 X 를 누른 것과 같은 WM_CLOSE)
  3. AtMessenger 프로세스를 강제로 끝낸다 (작업 관리자의 '작업 끝내기')
그 다음 가리기 프로세스가 검은 화면을 없앤다.
잠금을 먼저 하는 이유: 창을 닫을 때 뜨는 '저장할까요?' 창이나 앞으로 나오는 창이 검은 화면 위로 잠깐
비쳐 반짝이는데, 잠금 화면은 별도 화면이라 그 뒤에서 일어나는 일이 보이지 않는다.
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


def is_locked() -> bool:
    """잠금 화면(Winlogon 데스크톱)이 입력 화면이면 True. 사용자 화면에서는 'Default' 데스크톱이 입력을 받는다."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    desk = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    if not desk:
        return True  # 잠금 화면은 일반 프로그램이 열 수 없다
    try:
        name = ctypes.create_unicode_buffer(64)
        need = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(desk, 2, name, ctypes.sizeof(name), ctypes.byref(need)):  # UOI_NAME
            return False
        return name.value.lower() != "default"
    finally:
        user32.CloseDesktop(desk)


def wait_until(cond, timeout: float, interval: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while True:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(interval)


def run(close_wait: float = 1.5, lock_wait: float = 3.0) -> None:
    """비상종료 순서대로 실행: 잠금 → (잠금 화면 확인) → 창 닫기 → 메신저 끄기. 하나가 실패해도 다음은 한다."""
    keep = {os.getpid(), os.getppid()}  # 가리기 프로세스와 Apphelper 앱은 닫지 않는다
    steps = (lock_pc,
             lambda: wait_until(is_locked, lock_wait),  # 잠금 화면이 뜬 뒤에 창을 닫아야 반짝임이 안 보인다
             lambda: close_windows(keep),
             lambda: time.sleep(close_wait),
             kill_messenger)
    for step in steps:
        try:
            step()
        except Exception:  # noqa: BLE001
            pass
