"""UI 회귀 테스트 (화면 없이 offscreen 으로 실행)."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from apphelper.backend import DummyBackend  # noqa: E402
from apphelper.core.model import BasicAction, KeyCrash, Settings  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _overlay():
    for w in QtWidgets.QApplication.topLevelWidgets():
        if type(w).__name__ == "_Overlay" and w.isVisible():
            return w
    return None


def _run_modal(dlg, steps):
    """dlg.exec() 를 돌리면서 steps 를 순서대로(200ms 간격) 실행한다."""
    for i, fn in enumerate(steps, 1):
        QTimer.singleShot(200 * i, fn)
    QTimer.singleShot(200 * (len(steps) + 5), dlg.reject)  # 안전장치
    return dlg.exec()


def test_key_crash_pick_keeps_dialog_open(app):
    from apphelper.ui.dialogs import KeyCrashDialog

    dlg = KeyCrashDialog(KeyCrash("k"), DummyBackend())
    seen = {}

    def click():
        ov = _overlay()
        seen["overlay"] = ov is not None
        QTest.mouseClick(ov, Qt.MouseButton.LeftButton, pos=QPoint(321, 123))

    def check():
        seen["visible"] = dlg.isVisible()
        seen["coord"] = (dlg.base_x.value(), dlg.base_y.value())
        dlg._accept()

    result = _run_modal(dlg, [dlg._pick, click, check])
    assert seen == {"overlay": True, "visible": True, "coord": (321, 123)}
    assert result == 1  # 좌표 따기 후에도 OK 로 정상 종료
    assert dlg.kc.base_x == 321


def test_basic_action_pick_and_cancel(app):
    from apphelper.ui.dialogs import BasicActionDialog

    dlg = BasicActionDialog(BasicAction("a"), DummyBackend())
    seen = {}

    def click():
        QTest.mouseClick(_overlay(), Qt.MouseButton.LeftButton, pos=QPoint(10, 20))

    def cancel():
        QTest.keyClick(_overlay(), Qt.Key.Key_Escape)

    def check():
        seen["visible"] = dlg.isVisible()
        seen["steps"] = [s.describe() for s in dlg.steps()]
        dlg._accept()

    result = _run_modal(dlg, [dlg._add_crash_pick, click, dlg._add_crash_pick, cancel, check])
    assert seen == {"visible": True, "steps": ["crash (10, 20)"]}
    assert result == 1


def test_settings_test_publish_does_not_block(app):
    from apphelper.ui.dialogs import SettingsDialog

    def slow(_settings):
        time.sleep(0.5)
        return "ok"

    dlg = SettingsDialog(Settings(), slow, lambda: None)
    start = time.monotonic()
    dlg._test()
    assert time.monotonic() - start < 0.2  # 버튼을 눌러도 화면이 바로 돌아온다
    assert not dlg.test_btn.isEnabled()
