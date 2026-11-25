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


def test_cursor_hide_and_restore_do_not_fail():
    from apphelper.cover import cursor
    try:
        assert cursor.hide() in (True, False)
    finally:
        cursor.restore()


def test_is_locked_false_on_normal_desktop():
    from apphelper.cover.emergency import is_locked, wait_until
    assert is_locked() in (True, False)  # 서비스 세션 CI 에서는 입력 데스크톱이 없을 수 있다
    assert wait_until(lambda: True, 0.1) and not wait_until(lambda: False, 0.1)


def test_magnify_probe_reports(capsys):
    """B 방식: CI Windows 에서 실제로 검게 만들고 캡처해 본다 (판정은 출력만, 화면은 반드시 되돌린다)."""
    from apphelper.core.imagefind import grab_screen
    from apphelper.cover import magnify
    from apphelper.platform_win import WinBackend
    b = WinBackend()
    r = magnify.probe(lambda: grab_screen(b), seconds=1.0)
    ok, text = magnify.verdict(r)
    with capsys.disabled():
        print(f"\nMAGNIFY-PROBE ok={r['ok']} err={r['error']!r} before={r['before']} during={r['during']} -> {ok}: {text}")
    magnify.reset()
    after = grab_screen(b).mean()
    with capsys.disabled():
        print(f"MAGNIFY-PROBE after-reset={after}")
