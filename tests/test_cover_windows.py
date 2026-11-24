"""Windows 에서 실제 가리기 프로세스: 뜨는지, 캡처에서 빠지는지, STOP 으로 끝나는지."""
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 전용")


def test_guard_starts_is_excluded_from_capture_and_stops():
    import numpy as np

    from apphelper.cover import EXIT_STOPPED, CoverController
    from apphelper.cover.keys import make_verifier
    from apphelper.platform_win import WinBackend

    b = WinBackend()
    w, h = b.screen_size()
    before = b.grab_array(0, 0, w, h).astype(float)

    c = CoverController()
    c.start(make_verifier("abcd"))
    first = []
    t = threading.Thread(target=lambda: first.append(c.proc.stdout.readline().strip()), daemon=True)
    t.start()
    t.join(60)
    try:
        if not first or first[0].startswith("ERROR"):
            pytest.skip(f"가리기를 시작할 수 없는 환경: {first}")
        assert first[0] == "READY"
        after = b.grab_array(0, 0, w, h).astype(float)
        # 검은 창이 캡처에서 빠져 있으면 화면 캡처는 거의 그대로여야 한다
        if before.mean() > 10:
            assert after.mean() > before.mean() * 0.5, (before.mean(), after.mean())
    finally:
        c.stop(timeout=10)
    assert c.poll() == EXIT_STOPPED  # 해제 없이 STOP 으로 정상 종료 (후크·창 정리됨)
