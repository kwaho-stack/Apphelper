"""페이지에서 화면 가리기 + 상태, 네트워크 끊김 감지 → 자동 로그인."""
import json
import os
import sys

import pytest

from apphelper.core.model import AppData
from apphelper.service import Service
from tests.fakes import FakeScreen
from tests.test_results_publisher import FakeGitHub
from tests.test_service_remote import NOW, NOW_DT, last_tree


def make(gh=None, tmp_path=None, clock=None, **flags):
    d = AppData()
    s = d.settings
    s.github_owner, s.github_repo, s.github_token = "o", "r", "tok"
    for k, v in flags.items():
        setattr(s, k, v)
    return Service(d, FakeScreen(), "", transport=gh or FakeGitHub(), now=lambda: NOW_DT,
                   clock=clock or (lambda: NOW), remote_state_path=tmp_path / "rs.json" if tmp_path else None)


def test_cover_request_and_state_in_status(tmp_path):
    gh = FakeGitHub(request={"cover": {"id": "C1", "at": NOW * 1000}})
    svc = make(gh, tmp_path, remote_cover=True)
    got = []
    svc.on_cover_request = got.append
    svc.check_remote()
    svc.check_remote()
    assert got == ["C1"]
    svc.set_cover_state(True, request="C1")
    svc.request_publish = lambda: None
    svc.record_emergency()
    assert svc.send_status() is True
    cv = json.loads(last_tree(gh)[1]["status.json"])["cover"]
    assert cv["active"] and cv["request"] == "C1" and cv["since"] and cv["emergency_at"]
    gh.request = {"cover": {"id": "C2", "at": (NOW - 600) * 1000}}
    svc.check_remote()
    assert got == ["C1"]                                   # 오래된 요청은 무시
    assert make(gh, tmp_path, remote_cover=False).send_status() is False


def test_network_down_triggers_relogin_with_limits():
    t = {"now": 1000.0}
    svc = make(clock=lambda: t["now"])
    fired = []
    svc.on_net_relogin = lambda: fired.append(t["now"])
    assert [x.name for x in svc._relogin_targets()] == ["Genian"]  # 기본: Genian 만 켜짐
    for _ in range(4):                                      # 3분 안: 아직
        svc.net_result(False)
        t["now"] += 40
    assert fired == []
    svc.net_result(False)                                   # 160초째
    t["now"] += 30
    svc.net_result(False)                                   # 190초째 → 1번
    assert len(fired) == 1
    t["now"] += 60
    svc.net_result(False)
    assert len(fired) == 1                                  # 5분 간격
    for _ in range(4):
        t["now"] += 300
        svc.net_result(False)
    assert len(fired) == 3                                  # 한 번 끊긴 동안 최대 3번
    svc.net_result(True)                                    # 연결되면 다시 처음부터
    for _ in range(5):
        t["now"] += 60
        svc.net_result(False)
    assert len(fired) == 4
    svc.data.logins[0].net_relogin = False
    t["now"] += 1000
    svc.net_result(False)
    assert len(fired) == 4                                  # 그 옵션을 켠 항목이 없으면 안 함


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def tray(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import apphelper.app as appmod
    from apphelper.core.model import Store
    monkeypatch.setattr(appmod, "Store", lambda: Store(tmp_path / "data"))
    monkeypatch.setattr(appmod, "ask_new_cover_password", lambda parent=None, first=True: None)
    t = appmod.TrayApp(app)
    states = []
    monkeypatch.setattr(t.service, "set_cover_state", lambda *a, **k: states.append((a, k)))
    t.cover_states = states
    yield t
    t.cover.stop()
    t.service.stop()


def test_remote_cover_without_password_reports_and_shows_nothing(tray):
    tray.start_cover(remote_id="C1")
    assert not tray.cover.active and tray.editor is None
    (a, k), = tray.cover_states
    assert a == (False,) and "비밀번호" in k["error"] and k["request"] == "C1"


def test_remote_cover_starts_and_reports_end(tray):
    from apphelper.cover import CoverController
    tray.cover_password.set("abcd")
    tray.cover = CoverController([sys.executable, "-c", "import sys,time; sys.stdin.readline(); time.sleep(0.3)"])
    tray.start_cover(remote_id="C2")
    assert tray.cover.active and tray.cover_states[-1] == ((True,), {"request": "C2"})
    import time
    end = time.monotonic() + 10
    while tray.cover.active and time.monotonic() < end:
        time.sleep(0.05)
    tray._cover_tick()
    assert tray.cover_states[-1][0] == (False,)


def test_net_relogin_runs_only_marked_targets(tray, monkeypatch):
    ran = []
    monkeypatch.setattr(tray, "run_logins", lambda targets, run_id=None, by="app": ran.append(
        ([t.name for t in targets], by)))
    import threading
    real = threading.Thread

    class Now(real):
        def start(self):
            self.run()
    monkeypatch.setattr(threading, "Thread", Now)
    tray.start_net_relogin()
    assert ran == [(["Genian"], "net")]


def test_closeall_request_and_result_in_status(tmp_path):
    gh = FakeGitHub(request={"closeall": {"id": "X1", "at": NOW * 1000}})
    svc = make(gh, tmp_path, remote_closeall=True)
    got = []
    svc.on_closeall_request = got.append
    svc.check_remote()
    svc.check_remote()
    assert got == ["X1"]
    svc.set_closeall_state("X1", 4)
    assert svc.send_status() is True
    st = json.loads(last_tree(gh)[1]["status.json"])
    assert st["remote_closeall"] and st["closeall"]["count"] == 4 and st["closeall"]["request"] == "X1"
    gh.request = {"closeall": {"id": "X2", "at": (NOW - 600) * 1000}}
    svc.check_remote()
    assert got == ["X1"]                                    # 오래된 요청은 무시
    off = make(FakeGitHub(request={"closeall": {"id": "X3", "at": NOW * 1000}}), tmp_path)
    off.on_closeall_request = got.append
    off.check_remote()
    assert got == ["X1"]                                    # 허용이 꺼져 있으면 안 함


def test_app_close_all_windows_keeps_itself(tray, monkeypatch):
    import os
    import apphelper.app as appmod
    import apphelper.cover.emergency as em
    seen, results = [], []
    monkeypatch.setattr(appmod.sys, "platform", "win32")
    monkeypatch.setattr(em, "close_windows", lambda keep: seen.append(set(keep)) or 5)
    monkeypatch.setattr(tray.service, "set_closeall_state", lambda *a: results.append(a))
    tray.close_all_windows("X1")
    assert seen == [{os.getpid()}] and results == [("X1", 5)]
