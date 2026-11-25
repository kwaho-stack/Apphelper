"""화면 가리기: 앱 쪽 흐름 (가리기 프로세스는 가짜 명령으로 대신한다)."""
import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from apphelper.cover import CoverController  # noqa: E402


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
    monkeypatch.setattr(appmod, "ask_new_cover_password", lambda parent=None, first=True: None)
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
    tray.cover_password.set("abcd")
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
    tray.cover_password.set("abcd")
    tray.cover = fake(3, delay=0)
    tray.start_cover()
    assert wait_for(lambda: (tray._cover_tick(), bool(msgs))[1])
    assert "Windows 10 2004" in msgs[0] and tray.editor.isVisible()


def test_cover_asks_for_its_own_password_first(tray, monkeypatch):
    import apphelper.app as appmod
    tray.cover = fake(0)
    tray.start_cover()                      # 검은 화면 비밀번호를 정하지 않으면(취소) 시작하지 않는다
    assert not tray.cover.active and not tray.cover_password.is_set()
    monkeypatch.setattr(appmod, "ask_new_cover_password", lambda parent=None, first=True: "Cover-99")
    tray.start_cover()
    assert tray.cover.active and tray.cover_password.is_set()
    from apphelper.cover.keys import check
    assert check(tray.cover_password.verifier(), "Cover-99")
    tray.lock.set("Edit-1234")              # 편집 화면 비밀번호와는 별개
    assert not check(tray.cover_password.verifier(), "Edit-1234")


def test_emergency_exit_is_recorded_for_the_page(tray, monkeypatch):
    from apphelper.cover import EXIT_EMERGENCY
    published = []
    monkeypatch.setattr(tray.service, "request_publish", lambda: published.append(1))
    tray.cover_password.set("abcd")
    tray.cover = fake(EXIT_EMERGENCY, delay=0)
    tray.start_cover()
    assert wait_for(lambda: (tray._cover_tick(), tray.service.emergency_at is not None)[1])
    assert published and not (tray.editor and tray.editor.isVisible())  # 트레이에만 있는다
    assert tray.service.build_results() is not None
    tray.cover = fake(0)
    tray.start_cover()                      # 다음에 다시 가리면 지난 비상종료 표시는 지운다
    assert tray.service.emergency_at is None


def test_editor_has_cover_button(tray):
    tray.cover_password.set("abcd")
    tray._skip_lock_once = True
    tray.open_editor()
    buttons = [b.text() for b in tray.editor.findChildren(QtWidgets.QPushButton)]
    assert "화면 가리기" in buttons
