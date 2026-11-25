"""메신저 '가려져도 읽기': 메신저 창을 직접 찍은 그림에서 읽기, 창 이동, 설정 화면."""
import os
from datetime import datetime

import numpy as np
import pytest

from apphelper.core.crash import average_color
from apphelper.core.messenger import MessengerConfig, Point, WindowFrame, WindowRef, rebase


def window_image(w=60, h=40, color=(255, 255, 255)):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = color
    return img


def test_window_frame_reads_relative_to_window():
    img = window_image()
    img[10:15, 20:25] = (220, 40, 40)                # 창 안 (22, 12) 에 빨간 아이콘
    f = WindowFrame(img, (500, 300))                 # 좌표를 정할 때 창이 (500, 300) 에 있었다
    assert average_color(f, 522, 312, 3) == (220, 40, 40)
    with pytest.raises(ValueError):
        average_color(f, 400, 300, 3)                # 창 밖
    with pytest.raises(ValueError):
        average_color(WindowFrame(None, (0, 0)), 1, 1, 3)  # 못 찍음 → 확인불가


def test_rebase_moves_all_points_and_config_roundtrip():
    c = MessengerConfig(target=Point("a", 110, 120), others=[Point("b", 130, 140), Point("c", 0, 0)])
    c.anchor.x, c.anchor.y = 150, 160
    c.capture_window, c.window = True, WindowRef("AtMessenger.exe", "Cls", 100, 100)
    assert rebase(c, 90, 130) and not rebase(c, 90, 130)
    assert (c.target.x, c.target.y) == (100, 150) and (c.others[0].x, c.others[0].y) == (120, 170)
    assert (c.others[1].x, c.others[1].y) == (0, 0)  # 아직 안 정한 좌표는 그대로
    assert (c.anchor.x, c.anchor.y) == (140, 190) and (c.window.x, c.window.y) == (90, 130)
    d = MessengerConfig.from_dict(c.to_dict())
    assert d.capture_window and d.window == c.window
    assert not MessengerConfig.from_dict({}).capture_window  # 예전 설정은 꺼진 채로


def test_service_reads_covered_messenger_from_window(tmp_path):
    from apphelper.core.model import AppData
    from apphelper.service import Service
    from tests.fakes import FakeScreen
    img = window_image()
    img[10:15, 20:25] = (220, 40, 40)
    calls = []

    def grabber(ref):
        calls.append(ref.exe)
        return grabber.result

    grabber.result = (img, (700, 50))                # 창은 옮겨졌어도 창 안 위치로 읽는다
    d = AppData()
    m = d.messenger
    m.enabled, m.confirm_count, m.sample_radius = True, 1, 1
    m.target = Point("", 522, 312)
    m.capture_window, m.window = True, WindowRef("AtMessenger.exe", "", 500, 300)
    screen = FakeScreen(100, 60, (255, 255, 255))   # 화면에는 다른 창이 덮여 흰색
    svc = Service(d, screen, "", now=lambda: datetime(2026, 11, 25, 10, 0),
                  messenger_log_dir=tmp_path / "logs", window_grabber=grabber)
    svc.messenger_step()
    assert svc.messenger_snapshot()[0]["state"] == "온라인" and calls == ["AtMessenger.exe"]
    grabber.result = "메신저 창이 최소화되어 있어 읽을 수 없습니다"
    svc.messenger_step()
    assert svc.messenger_snapshot()[0]["state"] == "확인불가"


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_panel_turns_on_capture_and_follows_window_move(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import apphelper.ui.panels as panels
    from tests.test_ui_panels import MB, editor
    mb = MB()
    monkeypatch.setattr(panels, "QMessageBox", mb)
    ed = editor(tmp_path)
    mp = ed.messenger_panel
    c = ed.data.messenger
    img = window_image()
    img[10:15, 20:25] = (40, 40, 220)               # 자리비움(파랑)
    state = {"pos": (500, 300)}
    monkeypatch.setattr(panels, "window_at", lambda x, y: WindowRef("AtMessenger.exe", "Cls", *state["pos"]))
    monkeypatch.setattr(panels, "grab_messenger", lambda ref: (img, state["pos"]))

    mp.capture.setChecked(True)                      # 좌표가 없으면 켤 수 없다
    assert not c.capture_window and mb.warnings and not mp.capture.isChecked()
    c.target = Point("", 522, 312)
    mp.load()
    mp.capture.setChecked(True)
    assert c.capture_window and c.window == WindowRef("AtMessenger.exe", "Cls", 500, 300)
    assert "자리비움" in mp.target_now.text()

    state["pos"] = (100, 50)                         # 메신저 창을 옮김
    mp.refresh_target_now()
    assert (c.target.x, c.target.y) == (122, 62) and mp.tx.value() == 122
    assert "자리비움" in mp.target_now.text()

    monkeypatch.setattr(panels, "grab_messenger", lambda ref: "메신저 창이 최소화되어 있어 읽을 수 없습니다")
    mp.refresh_target_now()
    assert "최소화" in mp.target_now.text()

    shown = []
    monkeypatch.setattr(panels, "show_capture", lambda parent, text, image, mark: shown.append((text, mark)))
    monkeypatch.setattr(panels, "grab_messenger", lambda ref: (img, (100, 50)))
    mp._test_capture()
    assert "자리비움" in shown[0][0] and shown[0][1] == (22, 12)
