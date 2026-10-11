"""플랫폼별 ScreenBackend 선택. Windows 가 아니면 개발용 더미 백엔드를 쓴다."""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


class DummyBackend:
    """Windows 가 아닌 환경에서 UI 를 띄워 보기 위한 백엔드. 실제로 클릭하지 않는다."""

    def __init__(self, size: tuple[int, int] = (1920, 1080)):
        self.size = size
        self.cursor = (0, 0)

    def screen_size(self):
        return self.size

    def grab(self, left, top, width, height):
        return [[(0, 0, 0)] * width for _ in range(height)]

    def get_cursor(self):
        return self.cursor

    def set_cursor(self, x, y):
        self.cursor = (x, y)

    def click(self, x, y):
        log.debug("dummy crash (%d, %d)", x, y)
        self.cursor = (x, y)


def get_backend():
    if sys.platform == "win32":
        from .platform_win import WinBackend
        return WinBackend()
    return DummyBackend()
