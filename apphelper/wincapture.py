"""메신저 창 직접 캡처 (Windows 전용, ctypes).

Windows 는 창마다 자기 그림을 따로 그려 두므로, 다른 창에 가려져 있어도 PrintWindow(PW_RENDERFULLCONTENT) 로
그 창의 그림을 읽을 수 있다. 최소화된 창은 그림이 없어 읽을 수 없다.
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

from .core.messenger import WindowRef

PW_RENDERFULLCONTENT = 0x2
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DIB_RGB_COLORS, BI_RGB = 0, 0


class _Api:
    def __init__(self):
        from .platform_win import BITMAPINFO, gdi32, user32  # 같은 설정(argtypes)을 그대로 쓴다
        self.user32, self.gdi32, self.BITMAPINFO = user32, gdi32, BITMAPINFO
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = user32, self.kernel32
        u.WindowFromPoint.restype = wintypes.HWND
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]


_api: _Api | None = None


def api() -> _Api:
    global _api
    if sys.platform != "win32":
        raise OSError("Windows 전용입니다")
    if _api is None:
        _api = _Api()
    return _api


def _exe_name(pid: int) -> str:
    a = api()
    h = a.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not a.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        a.kernel32.CloseHandle(h)


def _info(hwnd) -> tuple[str, str, wintypes.RECT]:
    a = api()
    pid = wintypes.DWORD()
    a.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    cls = ctypes.create_unicode_buffer(256)
    a.user32.GetClassNameW(hwnd, cls, 256)
    rect = wintypes.RECT()
    a.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return _exe_name(pid.value), cls.value, rect


def window_at(x: int, y: int) -> WindowRef | None:
    """화면 (x, y) 에 보이는 창(맨 바깥 창)의 식별 정보와 지금 위치."""
    a = api()
    hwnd = a.user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd:
        return None
    root = a.user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    exe, cls, rect = _info(root)
    if not exe:
        return None
    return WindowRef(exe, cls, rect.left, rect.top)


def find_window(ref: WindowRef):
    """ref 와 같은 프로그램·종류의 보이는 창 중 가장 큰 것: (hwnd, 왼쪽 위 x, y, 최소화 여부). 없으면 None."""
    a = api()
    found = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if a.user32.IsWindowVisible(hwnd):
            exe, cls, rect = _info(hwnd)
            if exe.lower() == ref.exe.lower() and (not ref.cls or cls == ref.cls):
                area = (rect.right - rect.left) * (rect.bottom - rect.top)
                found.append((bool(a.user32.IsIconic(hwnd)), -area, hwnd, rect.left, rect.top))
        return True

    a.user32.EnumWindows(enum_proc(visit), 0)
    if not found:
        return None
    iconic, _, hwnd, x, y = min(found)  # 최소화 안 된 것 우선, 그중 가장 큰 창
    return hwnd, x, y, iconic


def capture_window(hwnd):
    """창 전체 그림을 (h, w, 3) RGB 배열로. 실패하면 None."""
    import numpy as np
    a = api()
    rect = wintypes.RECT()
    if not a.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    screen_dc = a.user32.GetDC(None)
    mem_dc = a.gdi32.CreateCompatibleDC(screen_dc)
    bmp = a.gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = a.gdi32.SelectObject(mem_dc, bmp)
    try:
        if not a.user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            return None
        a.gdi32.SelectObject(mem_dc, old)
        old = None
        info = a.BITMAPINFO()
        hd = info.bmiHeader
        hd.biSize = ctypes.sizeof(hd)
        hd.biWidth, hd.biHeight = w, -h
        hd.biPlanes, hd.biBitCount, hd.biCompression = 1, 32, BI_RGB
        buf = (ctypes.c_ubyte * (w * h * 4))()
        if not a.gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS):
            return None
    finally:
        if old is not None:
            a.gdi32.SelectObject(mem_dc, old)
        a.gdi32.DeleteObject(bmp)
        a.gdi32.DeleteDC(mem_dc)
        a.user32.ReleaseDC(None, screen_dc)
    return np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, w, 4)[:, :, 2::-1].copy()


def grab_messenger(ref: WindowRef):
    """(그림, 지금 창 위치 (x, y)) 또는 실패 이유 문자열."""
    if not ref.ready:
        return "메신저 창이 지정되지 않았습니다"
    try:
        found = find_window(ref)
        if found is None:
            return f"메신저 창({ref.exe})을 찾을 수 없습니다"
        hwnd, x, y, iconic = found
        if iconic:
            return "메신저 창이 최소화되어 있어 읽을 수 없습니다 (가려진 건 괜찮지만 최소화는 안 됨)"
        img = capture_window(hwnd)
        if img is None:
            return "메신저 창을 캡처하지 못했습니다"
        return img, (x, y)
    except OSError as e:
        return str(e)
