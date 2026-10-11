import pytest

from apphelper.core.crash import (
    CHANGED, UNCHANGED, average_color, clip_region, crash, is_changed, key_crash,
)
from tests.fakes import FakeScreen


def test_clip_region_center():
    assert clip_region(10, 5, 5, (20, 10)) == (8, 3, 5, 5)


def test_clip_region_top_left_edge():
    # (0,0) 중심 5x5 → 화면 안쪽 3x3 만
    assert clip_region(0, 0, 5, (20, 10)) == (0, 0, 3, 3)


def test_clip_region_bottom_right_edge():
    assert clip_region(19, 9, 5, (20, 10)) == (17, 7, 3, 3)


def test_clip_region_outside():
    assert clip_region(-10, -10, 5, (20, 10)) is None


def test_clip_region_invalid_size():
    with pytest.raises(ValueError):
        clip_region(1, 1, 0, (20, 10))


def test_average_color_uniform():
    s = FakeScreen(color=(10, 20, 30))
    assert average_color(s, 10, 5) == (10, 20, 30)


def test_average_color_mixed():
    s = FakeScreen()
    s.fill(8, 3, 5, 1, (250, 0, 0))  # 5x5 중 한 줄(5픽셀)만 빨강
    r, g, b = average_color(s, 10, 5)
    assert r == pytest.approx(50)
    assert (g, b) == (0, 0)
    assert s.grabs[-1] == (8, 3, 5, 5)


def test_average_color_edge_uses_inside_pixels_only():
    s = FakeScreen(color=(0, 0, 0))
    s.fill(0, 0, 1, 1, (90, 90, 90))  # 화면 안 3x3=9 픽셀 중 1개
    assert average_color(s, 0, 0) == (10, 10, 10)


def test_average_color_custom_size():
    s = FakeScreen()
    s.fill(10, 5, 1, 1, (90, 0, 0))
    assert average_color(s, 10, 5, size=3)[0] == pytest.approx(10)
    assert average_color(s, 10, 5, size=1)[0] == 90


@pytest.mark.parametrize("before,after,tol,expected", [
    ((100, 100, 100), (110, 100, 100), 10, False),   # 정확히 허용오차 → 안변함
    ((100, 100, 100), (110.1, 100, 100), 10, True),
    ((100, 100, 100), (100, 89, 100), 10, True),     # 음수 방향
    ((100, 100, 100), (105, 95, 108), 10, False),
    ((0, 0, 0), (0, 0, 3), 2, True),
])
def test_is_changed(before, after, tol, expected):
    assert is_changed(before, after, tol) is expected


def test_crash_restores_cursor():
    s = FakeScreen()
    s.cursor = (1, 2)
    crash(s, 10, 5)
    assert s.clicks == [(10, 5)]
    assert s.cursor == (1, 2)


def test_crash_offscreen_raises_without_click():
    s = FakeScreen()
    with pytest.raises(ValueError):
        crash(s, 100, 5)
    assert s.clicks == []


def test_key_crash_changed():
    s = FakeScreen(color=(0, 0, 0))
    s.on_click = lambda x, y: s.fill(x - 2, y - 2, 5, 5, (200, 200, 200))
    slept = []
    out = key_crash(s, 10, 5, wait_ms=1500, sleep=slept.append)
    assert out.changed and out.label == CHANGED
    assert out.before == (0, 0, 0) and out.after == (200, 200, 200)
    assert slept == [1.5]
    assert s.cursor == (3, 4)


def test_key_crash_unchanged_within_tolerance():
    s = FakeScreen(color=(50, 50, 50))
    s.on_click = lambda x, y: s.fill(x - 2, y - 2, 5, 5, (55, 45, 60))
    out = key_crash(s, 10, 5, tolerance=10, sleep=lambda _: None)
    assert not out.changed and out.label == UNCHANGED


def test_key_crash_custom_tolerance():
    s = FakeScreen(color=(50, 50, 50))
    s.on_click = lambda x, y: s.fill(x - 2, y - 2, 5, 5, (55, 50, 50))
    assert key_crash(s, 10, 5, tolerance=3, sleep=lambda _: None).changed
