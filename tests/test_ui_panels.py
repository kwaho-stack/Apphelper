"""메신저 상태·PC 전원 탭, 종료 알림, 트레이의 종료 실행."""
import json
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from apphelper.backend import DummyBackend  # noqa: E402
from apphelper.core.model import AppData  # noqa: E402
from apphelper.core.power import PowerScheduler  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class MB:
    StandardButton = QtWidgets.QMessageBox.StandardButton
    answer = QtWidgets.QMessageBox.StandardButton.Yes

    def __init__(self):
        self.infos, self.warnings, self.questions = [], [], []

    def information(self, _p, _t, text, *a):
        self.infos.append(text)

    def warning(self, _p, _t, text, *a):
        self.warnings.append(text)

    def question(self, _p, _t, text, *a):
        self.questions.append(text)
        return self.answer


@pytest.fixture
def mb(monkeypatch):
    import apphelper.ui.panels as panels
    fake = MB()
    monkeypatch.setattr(panels, "QMessageBox", fake)
    return fake


def editor(tmp_path, power=None, now_calls=None, saved=None):
    from apphelper.ui.editor import EditorWindow
    saved = saved if saved is not None else []
    return EditorWindow(AppData(), DummyBackend(), saved.append, lambda s: "", lambda: AppData(),
                        power=power or PowerScheduler(),
                        on_power_now=(now_calls.append if now_calls is not None else (lambda m: None)),
                        messenger_log_dir=tmp_path / "logs")


def test_messenger_panel_edits_config_and_autosaves(app, tmp_path, monkeypatch):
    import apphelper.ui.panels as panels
    saved = []
    ed = editor(tmp_path, saved=saved)
    mp = ed.messenger_panel
    picks = iter([(101, 202), (300, 400), (11, 22)])
    monkeypatch.setattr(panels, "pick_coordinate", lambda backend, hide: next(picks))
    monkeypatch.setattr(panels.QInputDialog, "getText", staticmethod(lambda *a, **k: ("한", True)))

    mp.enabled.setChecked(True)
    mp._pick_target()
    mp._pick_anchor()
    mp._add_other()
    mp.confirm.setValue(5)
    c = ed.data.messenger
    assert c.enabled and (c.target.x, c.target.y) == (101, 202)
    assert (c.anchor.x, c.anchor.y) == (300, 400) and c.anchor.rgb == (0, 0, 0)  # 더미 화면은 검은색
    assert [(p.name, p.x, p.y) for p in c.others] == [("한", 11, 22)] and c.confirm_count == 5
    assert mp.others.rowCount() == 1
    ed.flush()
    assert saved and saved[-1].messenger.others[0].name == "한"

    mp.others.setCurrentCell(0, 0)
    mp._delete_other()
    assert ed.data.messenger.others == []

    mp.show_live([{"name": "홍", "state": "온라인", "since": "2026-11-24 18:28:13"}])
    assert "온라인" in mp.live.text() and "18:28" in mp.live.text()


def test_messenger_panel_imports_mess_folder(app, tmp_path, monkeypatch, mb):
    import apphelper.ui.panels as panels
    src = tmp_path / "mess"
    (src / "logs").mkdir(parents=True)
    (src / "logs" / "2026-11-20.txt").write_text("2026-11-20 09:00:00  온라인\n", encoding="utf-8")
    (src / "config.json").write_text(json.dumps({"target": {"x": 5, "y": 6}, "others": []}), encoding="utf-8")
    monkeypatch.setattr(panels.QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(src)))
    ed = editor(tmp_path)
    ed.messenger_panel._import()
    assert ed.data.messenger.enabled and (ed.data.messenger.target.x, ed.data.messenger.target.y) == (5, 6)
    assert (tmp_path / "logs" / "2026-11-20.txt").exists()
    assert "기록 파일 1개" in mb.infos[0]


def test_power_panel_schedule_cancel_and_now(app, tmp_path, mb):
    power = PowerScheduler()
    now_calls = []
    ed = editor(tmp_path, power=power, now_calls=now_calls)
    pp = ed.power_panel
    pp.mode_restart.setChecked(True)
    pp._schedule_at()
    st = power.state()
    assert st["mode"] == "restart" and 0 < st["scheduled_at"] - time.time() <= 24 * 3600
    pp.refresh()
    assert "다시시작 예정" in pp.state.text()
    power.cancel()
    pp.refresh()
    assert pp.state.text() == "예약 없음"

    mb.answer = QtWidgets.QMessageBox.StandardButton.No
    pp._now("shutdown")
    assert now_calls == []
    mb.answer = QtWidgets.QMessageBox.StandardButton.Yes
    pp._now("shutdown")
    assert now_calls == ["shutdown"]

    pp.force.setChecked(True)
    assert ed.data.settings.power_force is True


def test_warning_dialog_cancel(app):
    from apphelper.ui.panels import PowerWarning
    power = PowerScheduler()
    power.schedule(time.time() + 50, "shutdown")
    w = PowerWarning(power)
    w.show()
    assert "초 후 컴퓨터를 종료합니다" in w.label.text()
    w._cancel()
    assert power.state()["scheduled_at"] is None and not w.isVisible()


def test_tray_power_fire_runs_shutdown_once(app, tmp_path, monkeypatch):
    import apphelper.app as appmod
    import apphelper.core.power as powermod
    from apphelper.core.model import Store

    monkeypatch.setattr(appmod, "Store", lambda: Store(tmp_path / "data"))
    calls = []
    monkeypatch.setattr(powermod, "shutdown_now", lambda force=False, restart=False: calls.append((force, restart)))
    t = appmod.TrayApp(app)
    try:
        t.data.settings.power_force = True
        t.power_now("restart")
        t._power_tick()
        assert calls == [(True, True)]
        assert t.service.power.state()["scheduled_at"] is None

        t.service.power.schedule(time.time() + 30, "shutdown")  # 1분 안 → 알림 창
        t._power_tick()
        assert t.warning is not None and t.warning.isVisible()
        t.warning._cancel()
        assert t.service.power.state()["scheduled_at"] is None
        assert [a.text() for a in t.menu.actions() if a.text()] == ["열기", "종료"]  # 트레이는 열기·종료만
    finally:
        t.service.stop()
