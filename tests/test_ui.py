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


class FakeMessageBox:
    """dialogs 모듈의 QMessageBox 대용. 실제 Qt 클래스는 건드리지 않는다."""
    StandardButton = QtWidgets.QMessageBox.StandardButton

    def __init__(self):
        self.infos, self.warnings = [], []

    def information(self, _parent, _title, text, *a):
        self.infos.append(text)

    def warning(self, _parent, _title, text, *a):
        self.warnings.append(text)

    def question(self, *a):
        return QtWidgets.QMessageBox.StandardButton.Yes


@pytest.fixture
def msgbox(monkeypatch):
    import apphelper.ui.dialogs as dialogs
    fake = FakeMessageBox()
    monkeypatch.setattr(dialogs, "QMessageBox", fake)
    return fake


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


def test_settings_test_publish_does_not_block(app, msgbox):
    from apphelper.ui.dialogs import SettingsDialog

    def slow(_settings):
        time.sleep(0.5)
        return "ok"

    messages = msgbox.infos
    dlg = SettingsDialog(Settings(), slow, lambda: None)
    start = time.monotonic()
    dlg._test()
    assert time.monotonic() - start < 0.2  # 버튼을 눌러도 화면이 바로 돌아온다
    assert not dlg.test_btn.isEnabled()
    deadline = time.monotonic() + 5
    while not messages and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert messages == ["ok"] and dlg.test_btn.isEnabled()


def test_key_crash_dialog_test_button_clicks(app, msgbox):
    from datetime import datetime

    from apphelper.ui.dialogs import KeyCrashDialog

    backend = DummyBackend()
    clicks = []
    backend.click = lambda x, y: clicks.append((x, y))
    kc = KeyCrash("k", base_hour=datetime.now().hour, base_x=50, base_y=60, wait_ms=0)
    dlg = KeyCrashDialog(kc, backend)
    messages = msgbox.infos
    dlg.show()
    pos = dlg.pos()
    dlg._test()
    deadline = time.monotonic() + 5
    while not messages and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert clicks == [(50, 60)]
    assert "안변함!" in messages[0]
    assert dlg.pos() == pos and dlg.test_btn.isEnabled()


def test_key_crash_clicks_even_if_color_read_fails():
    from apphelper.core.crash import key_crash

    backend = DummyBackend()
    clicks = []
    backend.click = lambda x, y: clicks.append((x, y))

    def broken(*a):
        raise OSError("BitBlt 실패")
    backend.grab = broken
    with pytest.raises(OSError):
        key_crash(backend, 5, 5, wait_ms=0)
    assert clicks == [(5, 5)]


class _ArrayBackend(DummyBackend):
    """numpy 배열을 화면으로 쓰는 더미 백엔드."""

    def __init__(self, arr):
        super().__init__((arr.shape[1], arr.shape[0]))
        self.arr = arr
        self.clicks = []

    def grab(self, left, top, width, height):
        return [[tuple(int(c) for c in px) for px in row]
                for row in self.arr[top:top + height, left:left + width]]

    def grab_array(self, left, top, width, height):
        return self.arr[top:top + height, left:left + width].copy()

    def click(self, x, y):
        self.clicks.append((x, y))


def _random_screen(h=300, w=400, seed=0):
    import numpy as np
    return np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)


def test_capture_region_returns_dragged_pixels(app):
    import numpy as np

    from apphelper.ui import picker

    screen = _random_screen()
    backend = _ArrayBackend(screen)
    got = {}

    def drag():
        ov = next(w for w in QtWidgets.QApplication.topLevelWidgets()
                  if type(w).__name__ == "_CaptureOverlay" and w.isVisible())
        # 오버레이(논리 좌표)와 화면 배열 크기가 다를 수 있으니 비율로 환산한다
        sx, sy = ov.width() / screen.shape[1], ov.height() / screen.shape[0]
        a, b = QPoint(round(50 * sx), round(40 * sy)), QPoint(round(90 * sx), round(70 * sy))
        QTest.mousePress(ov, Qt.MouseButton.LeftButton, pos=a)
        QTest.mouseMove(ov, b)
        QTest.mouseRelease(ov, Qt.MouseButton.LeftButton, pos=b)

    QTimer.singleShot(600, drag)
    got["rgb"] = picker.capture_region(backend)
    rgb = got["rgb"]
    assert rgb is not None and abs(rgb.shape[0] - 30) <= 2 and abs(rgb.shape[1] - 40) <= 2
    # 잘라낸 부분이 원래 화면에서 그대로 찾아져야 한다
    from apphelper.core.imagefind import best_match
    m = best_match(screen, rgb)
    assert m.score > 0.99 and abs(m.x - 70) <= 2 and abs(m.y - 55) <= 2
    assert np.array_equal(picker.qimage_to_rgb(picker.rgb_to_qimage(rgb)), rgb)


def test_key_crash_dialog_image_mode_test_clicks_found_center(app, msgbox):
    from apphelper.core.imagefind import encode_template
    from apphelper.ui.dialogs import KeyCrashDialog

    screen = _random_screen(seed=3)
    backend = _ArrayBackend(screen)
    kc = KeyCrash("img", mode="image", image=encode_template(screen[100:130, 200:240]), wait_ms=0)
    dlg = KeyCrashDialog(kc, backend)
    assert dlg.image_group.isVisibleTo(dlg) and not dlg.coord_form.isVisibleTo(dlg)
    messages = msgbox.infos
    dlg.show()
    dlg._test()
    deadline = time.monotonic() + 5
    while not messages and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert backend.clicks == [(220, 115)]
    assert "이미지 일치도 100%" in messages[0]
    dlg._accept()
    assert dlg.kc.uses_image and dlg.kc.image == kc.image


def test_key_crash_dialog_requires_image(app, msgbox):
    from apphelper.ui.dialogs import KeyCrashDialog

    warnings = msgbox.warnings
    dlg = KeyCrashDialog(KeyCrash("k"), DummyBackend())
    dlg.mode.setCurrentIndex(1)
    dlg._accept()
    assert warnings and dlg.result() == 0


def test_background_job_after_window_closed_is_dropped(app, msgbox):
    """작업 중에 창이 닫혀도 (이중 삭제 등으로) 죽지 않고 결과만 버린다."""
    import gc

    from apphelper.ui.dialogs import SettingsDialog

    def slow(_settings):
        time.sleep(0.2)
        return "late"

    dlg = SettingsDialog(Settings(), slow, lambda: None)
    dlg._test()
    del dlg
    gc.collect()
    end = time.monotonic() + 0.8
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    assert msgbox.infos == []
    w = QtWidgets.QWidget()  # 이후에도 Qt 가 정상 동작하는지
    w.show()
    app.processEvents()
