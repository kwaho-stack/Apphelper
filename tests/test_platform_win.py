import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 전용")


def test_win_backend_grab_and_average():
    from apphelper.core.crash import average_color, clip_region
    from apphelper.platform_win import WinBackend

    b = WinBackend()
    w, h = b.screen_size()
    assert w > 0 and h > 0
    rows = b.grab(0, 0, 5, 3)
    assert len(rows) == 3 and all(len(r) == 5 for r in rows)
    assert all(0 <= c <= 255 for r in rows for px in r for c in px)
    assert clip_region(0, 0, 5, (w, h)) == (0, 0, 3, 3)
    assert len(average_color(b, w - 1, h - 1)) == 3

