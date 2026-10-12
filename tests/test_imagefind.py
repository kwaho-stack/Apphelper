import numpy as np
import pytest

from apphelper.core.imagefind import (
    NotFound, best_match, decode_template, encode_template, locate, validate_template,
)
from apphelper.core.model import AppData, BlockRef, KeyCrash
from apphelper.core.runner import run_slot


def make_screen(seed=0, h=120, w=200):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


class ArrayScreen:
    """numpy 배열 화면. 클릭하면 on_click 으로 화면을 바꿀 수 있다."""

    def __init__(self, arr):
        self.arr = arr
        self.cursor = (0, 0)
        self.clicks = []
        self.on_click = None

    def screen_size(self):
        return self.arr.shape[1], self.arr.shape[0]

    def grab(self, left, top, width, height):
        return [[tuple(int(c) for c in px) for px in row]
                for row in self.arr[top:top + height, left:left + width]]

    def grab_array(self, left, top, width, height):
        return self.arr[top:top + height, left:left + width].copy()

    def get_cursor(self):
        return self.cursor

    def set_cursor(self, x, y):
        self.cursor = (x, y)

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click(x, y)


def test_encode_roundtrip():
    t = make_screen(1, 7, 9)
    assert np.array_equal(decode_template(encode_template(t)), t)


def test_best_match_exact_center():
    screen = make_screen()
    tpl = screen[40:60, 120:150]
    m = best_match(screen, tpl)
    assert (m.x, m.y) == (120 + 15, 40 + 10)
    assert m.score > 0.999


def test_best_match_tolerates_brightness_change():
    screen = make_screen(2)
    tpl = screen[10:30, 30:55].copy()
    shifted = np.clip(screen.astype(int) + 25, 0, 255).astype(np.uint8)
    m = best_match(shifted, tpl)
    assert (m.x, m.y) == (30 + 12, 10 + 10)
    assert m.score > 0.95


def test_locate_not_found():
    screen = make_screen(3)
    other = make_screen(4, 20, 20)
    with pytest.raises(NotFound) as e:
        locate(ArrayScreen(screen), other, 0.9)
    assert "일치도" in str(e.value)


def test_locate_works_without_grab_array():
    screen = make_screen(5, 40, 60)
    s = ArrayScreen(screen)
    s.grab_array = None
    del s.grab_array
    m = locate(s, screen[5:15, 20:32], 0.9)
    assert (m.x, m.y) == (26, 10)


def test_validate_template():
    assert validate_template(make_screen(6, 10, 10)) is None
    assert "작습니다" in validate_template(make_screen(6, 2, 10))
    assert "큽니다" in validate_template(make_screen(6, 10, 500))
    assert "한 가지 색" in validate_template(np.full((10, 10, 3), 50, np.uint8))


def test_run_slot_image_mode_clicks_match_center():
    screen = make_screen(7)
    tpl = screen[50:70, 80:100].copy()
    s = ArrayScreen(screen)
    s.on_click = lambda x, y: s.arr.__setitem__((slice(y - 2, y + 3), slice(x - 2, x + 3)), 255)
    d = AppData()
    kc = KeyCrash("img", mode="image", image=encode_template(tpl), base_x=1, base_y=1, dy=999)
    d.key_crashes.append(kc)
    d.slots[5].blocks = [BlockRef("key", kc.id)]
    res = run_slot(d, d.slots[5], s, sleep=lambda _: None)
    assert res.ok, res.error
    assert s.clicks == [(90, 60)]  # 시간별 변동(dy)은 이미지 모드에서 쓰지 않는다
    assert res.key_results == ["변함!"]


def test_run_slot_image_mode_not_found_fails_without_click():
    s = ArrayScreen(make_screen(8))
    d = AppData()
    kc = KeyCrash("img", mode="image", image=encode_template(make_screen(9, 20, 20)))
    d.key_crashes.append(kc)
    d.slots[5].blocks = [BlockRef("key", kc.id)]
    res = run_slot(d, d.slots[5], s, sleep=lambda _: None)
    assert not res.ok and "찾지 못했습니다" in res.error
    assert s.clicks == []
