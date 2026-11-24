"""윈도우 시작 시 자동 실행 (기존 closet 기능). 켤 때만 HKCU\\...\\Run 에 값 하나를 쓰고, 끄거나
전체 데이터 삭제를 하면 지운다. exe 로 실행할 때만 지원한다."""
from __future__ import annotations

import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "Apphelper"


def supported() -> bool:
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def enabled() -> bool:
    if not supported():
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE)
            return True
    except OSError:
        return False


def set_enabled(on: bool) -> None:
    if not supported():
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, VALUE)
            except OSError:
                pass
