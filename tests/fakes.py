from __future__ import annotations


class FakeScreen:
    """픽셀 버퍼를 가진 가짜 화면. click 시 on_click 콜백으로 화면을 바꿀 수 있다."""

    def __init__(self, width=20, height=10, color=(0, 0, 0)):
        self.width, self.height = width, height
        self.pixels = [[color for _ in range(width)] for _ in range(height)]
        self.cursor = (3, 4)
        self.clicks: list[tuple[int, int]] = []
        self.on_click = None
        self.grabs: list[tuple[int, int, int, int]] = []

    def fill(self, left, top, width, height, color):
        for y in range(top, top + height):
            for x in range(left, left + width):
                if 0 <= x < self.width and 0 <= y < self.height:
                    self.pixels[y][x] = color

    def screen_size(self):
        return self.width, self.height

    def grab(self, left, top, width, height):
        assert left >= 0 and top >= 0 and left + width <= self.width and top + height <= self.height
        self.grabs.append((left, top, width, height))
        return [row[left:left + width] for row in self.pixels[top:top + height]]

    def get_cursor(self):
        return self.cursor

    def set_cursor(self, x, y):
        self.cursor = (x, y)

    def click(self, x, y):
        self.cursor = (x, y)
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click(x, y)
