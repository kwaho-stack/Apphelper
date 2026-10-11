"""crash(클릭)와 핵심 crash(클릭 전후 영역 평균 색상 비교) 핵심 로직.

화면 입출력은 ScreenBackend 프로토콜로 추상화한다. 실제 Windows 구현은
apphelper.platform_win 에 있고, 테스트에서는 가짜 백엔드를 쓴다.
픽셀 값은 메모리에서만 다루며 스크린샷 파일은 만들지 않는다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

RGB = tuple[float, float, float]

CHANGED = "변함!"
UNCHANGED = "안변함!"


class ScreenBackend(Protocol):
    def screen_size(self) -> tuple[int, int]:
        """주 모니터 크기 (width, height)."""

    def grab(self, left: int, top: int, width: int, height: int) -> Sequence[Sequence[tuple[int, int, int]]]:
        """화면 영역의 픽셀을 행 단위 RGB 목록으로 돌려준다. 영역은 항상 화면 안쪽이다."""

    def get_cursor(self) -> tuple[int, int]:
        """현재 마우스 커서 위치."""

    def set_cursor(self, x: int, y: int) -> None:
        """마우스 커서를 옮긴다."""

    def click(self, x: int, y: int) -> None:
        """(x, y)에서 왼쪽 버튼을 1회 누르고 뗀다."""


def is_on_screen(x: int, y: int, screen: tuple[int, int]) -> bool:
    return 0 <= x < screen[0] and 0 <= y < screen[1]


def clip_region(cx: int, cy: int, size: int, screen: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """(cx, cy) 중심 size×size 영역을 화면 안으로 잘라 (left, top, width, height)를 돌려준다.

    짝수 크기면 중심 기준 왼쪽/위쪽으로 한 칸 더 포함한다. 화면과 겹치는 부분이 없으면 None.
    """
    if size < 1:
        raise ValueError("영역 크기는 1 이상이어야 합니다")
    half = size // 2
    left, top = cx - half, cy - half
    right, bottom = left + size, top + size  # exclusive
    left, top = max(left, 0), max(top, 0)
    right, bottom = min(right, screen[0]), min(bottom, screen[1])
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top


def average_color(backend: ScreenBackend, x: int, y: int, size: int = 5) -> RGB:
    """(x, y) 중심 size×size 영역 중 화면 안쪽 픽셀의 평균 RGB."""
    region = clip_region(x, y, size, backend.screen_size())
    if region is None:
        raise ValueError(f"좌표 ({x}, {y}) 영역이 화면 밖입니다")
    rows = backend.grab(*region)
    total_r = total_g = total_b = 0
    count = 0
    for row in rows:
        for r, g, b in row:
            total_r += r
            total_g += g
            total_b += b
            count += 1
    if count == 0:
        raise ValueError("읽은 픽셀이 없습니다")
    return total_r / count, total_g / count, total_b / count


def is_changed(before: RGB, after: RGB, tolerance: float = 10) -> bool:
    """채널별 차이 중 하나라도 허용오차를 넘으면 True."""
    return any(abs(a - b) > tolerance for a, b in zip(before, after))


def crash(backend: ScreenBackend, x: int, y: int) -> None:
    """(x, y)를 1회 crash 하고 커서를 원래 위치로 되돌린다."""
    if not is_on_screen(x, y, backend.screen_size()):
        raise ValueError(f"crash 좌표 ({x}, {y})가 화면 밖입니다")
    original = backend.get_cursor()
    try:
        backend.click(x, y)
    finally:
        backend.set_cursor(*original)


@dataclass(frozen=True)
class KeyCrashOutcome:
    before: RGB
    after: RGB
    changed: bool

    @property
    def label(self) -> str:
        return CHANGED if self.changed else UNCHANGED


def key_crash(
    backend: ScreenBackend,
    x: int,
    y: int,
    wait_ms: int = 1500,
    size: int = 5,
    tolerance: float = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> KeyCrashOutcome:
    """핵심 crash: 색상 읽기 → crash → 대기 → 다시 읽기 → 비교."""
    before = average_color(backend, x, y, size)
    crash(backend, x, y)
    sleep(max(wait_ms, 0) / 1000)
    after = average_color(backend, x, y, size)
    return KeyCrashOutcome(before, after, is_changed(before, after, tolerance))
