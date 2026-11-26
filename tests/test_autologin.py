"""자동 로그인: 열린 창에 로그인, 없으면 열고 로그인, 설정 화면."""
import os
import time

import pytest

from apphelper.core.autologin import LoginTarget, Win, check, protect, run_login, unprotect
from apphelper.core.model import AppData


class FakeOps:
    def __init__(self, open_after=None, start_open=False, pos=(100, 200)):
        self.log, self.open, self.open_after, self.pos, self.slept = [], start_open, open_after, pos, 0.0

    def find(self, t):
        if not self.open and self.open_after is not None and self.slept >= self.open_after and "launch" in self.log:
            self.open = True
        return Win("h", *self.pos) if self.open else None

    def launch(self, t):
        self.log.append("launch")

    def focus(self, w):
        self.log.append("focus")
        return Win(w.hwnd, 300, 400)      # 앞으로 가져온 뒤의 실제 위치

    def arrange(self, w, layout):
        self.log.append(("arrange", layout))
        return Win(w.hwnd, 960, 0)        # 오른쪽 절반으로 옮긴 뒤의 위치

    def click(self, x, y):
        self.log.append(("click", x, y))

    def select_all(self):
        self.log.append("ctrl+a")

    def type_text(self, s):
        self.log.append(("type", s))

    def key(self, k):
        self.log.append(k)

    def sleep(self, s):
        self.slept += s


def target(**kw):
    t = LoginTarget(name="메신저", exe="AtMessenger.exe", user="me", path=r"C:\At\AtMessenger.exe", **kw)
    t.password = "pa ss"
    return t


def test_open_window_logs_in_with_tab():
    ops = FakeOps(start_open=True)
    r = run_login(target(), ops)
    assert r.ok and "열린 창" in r.message
    assert ops.log == ["focus", "ctrl+a", ("type", "me"), "tab", "ctrl+a", ("type", "pa ss"), "enter"]


def test_launches_when_closed_and_clicks_fields_relative_to_window():
    ops = FakeOps(open_after=2.0)
    r = run_login(target(id_x=10, id_y=20, pw_x=10, pw_y=50, press_enter=False), ops)
    assert r.ok and "새로 열어서" in r.message
    assert ops.log[0] == "launch" and ("click", 310, 420) in ops.log and ("click", 310, 450) in ops.log
    assert "tab" not in ops.log and "enter" not in ops.log


def test_failures_are_explained():
    assert not run_login(target(launch_wait=3), FakeOps(open_after=None)).ok        # 창이 안 뜸
    t = target()
    t.path = ""
    assert "경로" in run_login(t, FakeOps()).message
    assert check(LoginTarget(user="a")) == "비밀번호를 입력하세요"
    assert check(LoginTarget(kind="web", user="a", secret=protect("b"))) == "웹 주소나 창 제목 글자를 입력하세요"
    web = LoginTarget(kind="web", user="a", url="https://x", title="X")
    web.password = "b"
    assert check(web) is None


def test_password_protected_and_saved_with_appdata():
    t = target()
    assert "pa ss" not in t.secret and unprotect(t.secret) == "pa ss"
    d = AppData()
    assert [x.name for x in d.logins] == ["Genian", "웹 사이트", "AT메신저"]
    d.logins = [t]
    d2 = AppData.from_dict(d.to_dict())
    assert d2.logins[0].password == "pa ss" and d2.logins[0].exe == "AtMessenger.exe"
    assert "pa ss" not in str(d.to_dict())


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_login_panel_edit_pick_and_run(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import apphelper.ui.panels as panels
    from apphelper.backend import DummyBackend
    from apphelper.ui.editor import EditorWindow
    saved, runs = [], []
    ed = EditorWindow(AppData(), DummyBackend(), saved.append, lambda s: "", lambda: AppData(),
                      on_login=lambda ts, progress=None: runs.append([t.name for t in ts]) or "✅ 완료")
    lp = ed.login_panel
    assert lp is not None and ed.tabs.tabText(ed.tabs.count() - 1) == "자동 로그인"
    assert lp.list.count() == 3
    lp.list.setCurrentRow(2)
    lp.user.setText("kim")
    lp.user.editingFinished.emit()
    lp.pw.setText("secret!")
    lp.pw.editingFinished.emit()
    t = ed.data.logins[2]
    assert t.user == "kim" and t.password == "secret!" and lp.pw.text() == ""
    monkeypatch.setattr(panels, "pick_coordinate", lambda backend, hide: (150, 260))
    monkeypatch.setattr(panels, "window_info_at",
                        lambda x, y: {"exe": "AtMessenger.exe", "title": "AT", "x": 100, "y": 200})
    lp._pick_field("id")
    assert (t.id_x, t.id_y) == (50, 60) and "50, 60" in lp.id_pos.text()
    lp._add("web")
    assert ed.data.logins[-1].kind == "web" and lp.url.isVisibleTo(lp)
    lp._pick_window()
    assert ed.data.logins[-1].title == "AT" and ed.data.logins[-1].exe == "AtMessenger.exe"
    lp.list.item(0).setCheckState(panels.Qt.CheckState.Unchecked)
    assert not ed.data.logins[0].enabled
    lp.run_all_btn.click()
    end = time.monotonic() + 5
    while "완료" not in lp.result.toPlainText() and time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)
    assert runs == [["웹 사이트", "AT메신저", "새 웹 사이트"]] and lp.run_all_btn.isEnabled()
    ed.flush()
    assert saved[-1].logins[2].user == "kim"
    ed.close()


def test_layout_before_typing_uses_arranged_position():
    ops = FakeOps(start_open=True)
    r = run_login(target(layout="right", id_x=10, id_y=20), ops)
    assert r.ok and ops.log[:2] == ["focus", ("arrange", "right")] and ("click", 970, 20) in ops.log
    assert LoginTarget.from_dict({"layout": "weird"}).layout == "none"


def test_app_runs_in_order_and_reports_progress(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import apphelper.app as appmod
    from apphelper.core.model import Store
    monkeypatch.setattr(appmod, "Store", lambda: Store(tmp_path / "data"))
    tray = appmod.TrayApp(app)
    states, lines = [], []
    monkeypatch.setattr(tray.service, "set_login_state", lambda st: states.append(st))
    monkeypatch.setattr(tray, "_login_ops", lambda: FakeOps(start_open=True))
    a, b = target(), target()
    a.name, b.name = "Genian", "AT"
    b.user = ""                                          # 두 번째는 설정이 빠져 실패
    text = tray.run_logins([a, b], progress=lines.append, run_id="r1", by="page")
    seq = [[i["state"] for i in st["items"]] for st in states]
    assert seq[0] == ["대기", "대기"] and ["로그인 중", "대기"] in seq and ["완료", "로그인 중"] in seq
    assert seq[-1] == ["완료", "실패"] and states[-1]["done"] and states[-1]["id"] == "r1"
    assert "1. Genian — ✅ 완료" in text and "2. AT — ❌ 실패" in text and lines[-1] == text
    tray._login_lock.acquire()
    assert "이미" in tray.run_logins([a])                # 동시에 두 번은 안 돈다
    tray._login_lock.release()
    tray.service.stop()


def test_service_login_request(tmp_path):
    from tests.test_results_publisher import FakeGitHub
    from tests.test_service_remote import NOW, NOW_DT, last_tree
    import json
    from apphelper.service import Service
    from tests.fakes import FakeScreen
    gh = FakeGitHub(request={"login": {"id": "L1", "at": NOW * 1000}})
    d = AppData()
    d.settings.github_owner, d.settings.github_repo, d.settings.github_token = "o", "r", "tok"
    d.settings.remote_login = True
    svc = Service(d, FakeScreen(), "", transport=gh, now=lambda: NOW_DT, clock=lambda: NOW,
                  remote_state_path=tmp_path / "rs.json")
    got = []
    svc.on_login_request = got.append
    svc.check_remote()
    svc.check_remote()
    assert got == ["L1"]                                  # 같은 요청은 한 번만
    svc.set_login_state({"id": "L1", "done": False, "items": [{"name": "G", "state": "로그인 중", "msg": ""}]})
    assert svc._kick.is_set() and svc.send_status() is True
    st = json.loads(last_tree(gh)[1]["status.json"])
    assert st["remote_login"] and st["login"]["items"][0]["state"] == "로그인 중"
    gh.request = {"login": {"id": "L2", "at": (NOW - 600) * 1000}}
    svc.check_remote()
    assert got == ["L1"]                                  # 오래된 요청은 실행하지 않는다
