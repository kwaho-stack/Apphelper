"""Windows 화면/마우스 백엔드 (ctypes, 외부 의존성 없음). 단일 주 모니터 기준."""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("platform_win 은 Windows 전용입니다")

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SM_CXSCREEN, SM_CYSCREEN = 0, 1
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


class WinBackend:
    """ScreenBackend 구현. 픽셀은 메모리 버퍼로만 읽고 파일로 저장하지 않는다."""

    has_real_cursor = True
    click_hold = 0.03  # 버튼 누르고 있는 시간(초)

    def screen_size(self) -> tuple[int, int]:
        return user32.GetSystemMetrics(SM_CXSCREEN), user32.GetSystemMetrics(SM_CYSCREEN)

    def get_cursor(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def set_cursor(self, x: int, y: int) -> None:
        user32.SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int) -> None:
        self.set_cursor(x, y)
        time.sleep(0.01)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(self.click_hold)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def grab(self, left: int, top: int, width: int, height: int):
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmp = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        old = gdi32.SelectObject(mem_dc, bmp)
        try:
            if not gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY):
                raise OSError("BitBlt 실패")
            info = BITMAPINFO()
            h = info.bmiHeader
            h.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            h.biWidth, h.biHeight = width, -height  # 음수 = 위에서 아래로
            h.biPlanes, h.biBitCount, h.biCompression = 1, 32, BI_RGB
            buf = (ctypes.c_ubyte * (width * height * 4))()
            gdi32.SelectObject(mem_dc, old)
            if not gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(info), DIB_RGB_COLORS):
                raise OSError("GetDIBits 실패")
            old = None
        finally:
            if old is not None:
                gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)
        rows = []
        for yy in range(height):
            base = yy * width * 4
            rows.append([(buf[base + i + 2], buf[base + i + 1], buf[base + i])  # BGRA → RGB
                         for i in range(0, width * 4, 4)])
        return rows
