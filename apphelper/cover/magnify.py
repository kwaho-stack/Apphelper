"""화면 가리기 방법 B: 화면 출력 전체를 검게 (Windows 돋보기·색상 필터와 같은 Magnification API).

창이 아니라 화면 출력 단계에서 색을 바꾸므로 선택 상자 목록·메뉴·알림 같은 팝업도 뚫고 나오지 못한다.
화면 캡처(색 비교·이미지 찾기·스크린샷)가 원래 화면을 보는지는 PC 마다 시험해야 한다 (probe()).
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes


class MAGCOLOREFFECT(ctypes.Structure):
    _fields_ = [("transform", ctypes.c_float * 25)]  # 5x5, 행 우선. 출력 = [r g b a 1] × 행렬


def _matrix(values) -> MAGCOLOREFFECT:
    m = MAGCOLOREFFECT()
    for i, v in enumerate(values):
        m.transform[i] = v
    return m


IDENTITY = [1.0 if i % 6 == 0 else 0.0 for i in range(25)]
BLACK = [1.0 if i in (18, 24) else 0.0 for i in range(25)]  # 색은 0, 알파와 상수항만 그대로


class Magnifier:
    """한 스레드에서 init → black → restore 순서로 쓴다."""

    def __init__(self):
        self.dll = None
        self.active = False

    def _load(self) -> bool:
        if sys.platform != "win32":
            return False
        if self.dll is None:
            try:
                dll = ctypes.WinDLL("Magnification", use_last_error=True)
            except OSError:
                return False
            dll.MagInitialize.restype = wintypes.BOOL
            dll.MagUninitialize.restype = wintypes.BOOL
            dll.MagSetFullscreenColorEffect.restype = wintypes.BOOL
            dll.MagSetFullscreenColorEffect.argtypes = [ctypes.POINTER(MAGCOLOREFFECT)]
            self.dll = dll
        return True

    def black(self) -> tuple[bool, str]:
        """화면 출력을 검게. (성공, 실패 이유)."""
        if not self._load():
            return False, "Magnification.dll 을 쓸 수 없습니다"
        if not self.dll.MagInitialize():
            return False, f"MagInitialize 실패 ({ctypes.get_last_error()})"
        self.active = True
        if not self.dll.MagSetFullscreenColorEffect(ctypes.byref(_matrix(BLACK))):
            err = ctypes.get_last_error()
            self.restore()
            return False, f"MagSetFullscreenColorEffect 실패 ({err})"
        return True, ""

    def restore(self) -> None:
        if not self.active or self.dll is None:
            return
        try:
            self.dll.MagSetFullscreenColorEffect(ctypes.byref(_matrix(IDENTITY)))
        finally:
            self.dll.MagUninitialize()
            self.active = False


def reset() -> None:
    """혹시 검게 남아 있으면 원래대로 (앱 시작·가리기 종료 때 한 번 더)."""
    m = Magnifier()
    if m._load() and m.dll.MagInitialize():
        m.active = True
        m.restore()


def probe(grab, seconds: float = 2.0) -> dict:
    """B 방식 시험: 검게 만든 상태에서 화면을 캡처해 원래 화면이 찍히는지 본다.

    grab() → numpy 배열 (화면 전체). 반환: ok, error, before(평균 밝기), during, image(검게 한 동안 찍은 배열)
    """
    before = grab()
    m = Magnifier()
    ok, err = m.black()
    if not ok:
        return {"ok": False, "error": err, "before": float(before.mean()), "during": None, "image": None}
    try:
        time.sleep(seconds)
        during = grab()
    finally:
        m.restore()
    return {"ok": True, "error": "", "before": float(before.mean()), "during": float(during.mean()),
            "image": during}


def verdict(result: dict) -> tuple[bool, str]:
    """probe 결과 → (B 를 쓸 수 있음, 설명)."""
    if not result["ok"]:
        return False, f"이 PC 에서는 화면 출력을 검게 만들 수 없습니다: {result['error']}"
    before, during = result["before"], result["during"]
    if before < 8:
        return False, (f"시험 전 화면이 너무 어두워 판단할 수 없습니다 (밝기 {before:.0f}). "
                       "밝은 창을 띄워 놓고 다시 시험하세요.")
    if during < max(4.0, before * 0.2):
        return False, (f"검게 한 동안 캡처도 검게 찍혔습니다 (밝기 {before:.0f} → {during:.0f}). "
                       "이 PC 에서는 B 방식을 쓰면 매크로·감시가 화면을 못 봅니다.")
    return True, (f"검게 한 동안에도 캡처에는 원래 화면이 찍혔습니다 (밝기 {before:.0f} → {during:.0f}). "
                  "B 방식을 쓸 수 있습니다.")
