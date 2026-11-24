"""화면 가리기: 앱 쪽 흐름 (가리기 프로세스는 가짜 명령으로 대신한다)."""
import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from apphelper.cover import CoverController  # noqa: E402
from apphelper.cover.keys import make_verifier  # noqa: E402


def fake(code: int, delay: float = 0.2) -> CoverController:
    """검증값 한 줄을 읽고 delay 뒤 code 로 끝나는 가짜 가리기 프로세스."""
    script = f"import sys, json, time; v = json.loads(sys.stdin.readline()); assert v['hash']; " \
             f"time.sleep({delay}); sys.exit({code})"
    return CoverController([sys.executable, "-c", script])


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def tray(app, tmp_path, monkeypatch):
    import apphelper.app as appmod
    from apphelper.core.model import Store
    monkeypatch.setattr(appmod, "Store", lambda: Store(tmp_path / "data"))
    asked = []
    monkeypatch.setattr(appmod, "ask_password", lambda parent=None, retry=False: asked.append(1) or None)
    monkeypatch.setattr(appmod, "ask_new_password", lambda parent=None, first=True: None)  # 실제 창이 뜨지 않게
    t = appmod.TrayApp(app)
    t.asked = asked
    yield t
    t.cover.stop()
    t.service.stop()
    if t.editor:
        t.editor.close()


def wait_for(cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_cover_hides_editor_and_unlock_stays_in_tray(tray, monkeypatch):
    from apphelper.cover import cursor
    restored = []
    monkeypatch.setattr(cursor, "restore", lambda: restored.append(1))
    tray.verifier = make_verifier("abcd")
    tray._skip_lock_once = True
    tray.open_editor()
    assert tray.editor.isVisible()
    tray.cover = fake(0)
    tray.start_cover()
    assert not tray.editor.isVisible() and tray.cover.active
    tray.open_editor()                       # 가린 동안에는 열리지 않는다
    assert not tray.editor.isVisible()
    assert wait_for(lambda: (tray._cover_tick(), not tray.cover.active and bool(restored))[1])
    assert not tray.editor.isVisible()       # 풀린 뒤에도 트레이에만 있다
    assert tray.asked == []
    tray.lock.set("abcd")
    tray.open_editor()                       # 다시 열려면 평소처럼 비밀번호를 묻는다
    assert tray.asked == [1] and not tray.editor.isVisible()


def test_unsupported_windows_shows_message_and_editor(tray, monkeypatch):
    msgs = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a: msgs.append(a[2])))
    tray.verifier = make_verifier("abcd")
    tray.cover = fake(3, delay=0)
    tray.start_cover()
    assert wait_for(lambda: (tray._cover_tick(), bool(msgs))[1])
    assert "Windows 10 2004" in msgs[0] and tray.editor.isVisible()


def test_untypable_password_refuses(tray, monkeypatch):
    msgs = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a: msgs.append(a[2])))
    tray.verifier = make_verifier("한글비번")
    tray.cover = fake(0)
    tray.start_cover()
    assert not tray.cover.active and "영문 자판" in msgs[0]


def test_editor_has_cover_button(tray):
    tray.verifier = make_verifier("abcd")
    tray._skip_lock_once = True
    tray.open_editor()
    buttons = [b.text() for b in tray.editor.findChildren(QtWidgets.QPushButton)]
    assert "화면 가리기" in buttons
