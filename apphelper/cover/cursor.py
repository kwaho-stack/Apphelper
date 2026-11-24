"""화면 가리기 중 마우스 커서 숨기기 (Windows).

검은 창은 클릭이 통과하는 창이라 커서 모양을 정할 수 없으므로, 가리는 동안 시스템 커서를
모두 투명 커서로 바꾸고 끝나면 사용자 설정 커서로 되돌린다.
가리기 프로세스가 강제 종료돼 되돌리지 못한 경우에 대비해 앱도 끝났을 때·시작할 때 되돌린다.
"""
from __future__ import annotations

import sys

# 표준 커서 id (OCR_*): 기본, I빔, 대기, 십자, 위, 크기 조절 4종, 이동, 금지, 손, 백그라운드 작업, 도움말
CURSOR_IDS = (32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644, 32645, 32646, 32648, 32649, 32650, 32651)
SPI_SETCURSORS = 0x57


def _user32():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.CreateCursor.restype = wintypes.HANDLE
    user32.CreateCursor.argtypes = [wintypes.HINSTANCE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_void_p, ctypes.c_void_p]
    user32.SetSystemCursor.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    return ctypes, user32


def hide() -> bool:
    """모든 표준 커서를 투명 커서로 바꾼다. 성공하면 True."""
    if sys.platform != "win32":
        return False
    ctypes, user32 = _user32()
    w = h = 32
    and_mask = (ctypes.c_ubyte * (w * h // 8))(*([0xFF] * (w * h // 8)))  # AND 1 = 화면 그대로
    xor_mask = (ctypes.c_ubyte * (w * h // 8))()                          # XOR 0 = 아무것도 안 그림
    ok = True
    for cid in CURSOR_IDS:
        cur = user32.CreateCursor(None, 0, 0, w, h, and_mask, xor_mask)  # SetSystemCursor 가 소유·파기
        ok = bool(cur and user32.SetSystemCursor(cur, cid)) and ok
    return ok


def restore() -> None:
    """사용자 설정 커서로 되돌린다 (가리지 않은 상태에서 불러도 무해)."""
    if sys.platform != "win32":
        return
    _, user32 = _user32()
    user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0)
