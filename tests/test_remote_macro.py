"""결과 페이지에서 매크로 조정: 켜기/끄기, 프로필, 시간대 사용·분 범위."""
import json
import os

import pytest

from apphelper.core.model import AppData
from apphelper.service import Service
from tests.fakes import FakeScreen
from tests.test_results_publisher import FakeGitHub
from tests.test_service_remote import NOW, NOW_DT, last_tree


def make(gh, tmp_path, remote_macro=True):
    d = AppData()
    s = d.settings
    s.github_owner, s.github_repo, s.github_token = "o", "r", "tok"
    s.remote_macro = remote_macro
    return Service(d, FakeScreen(), "<html>", transport=gh, now=lambda: NOW_DT,
                   remote_state_path=tmp_path / "remote.json", clock=lambda: NOW)


def op(i, kind, age_s=0, **kw):
    return {"id": i, "at": (NOW - age_s) * 1000, "op": kind, **kw}


def test_new_ops_go_to_app_once_and_ack_after_apply(tmp_path):
    gh = FakeGitHub(request={"macro": [op("a", "running", value=False), op("old", "running", age_s=3600, value=True)]})
    svc = make(gh, tmp_path)
    got = []
    svc.on_macro_ops = got.append
    svc.check_remote()
    assert [o["id"] for o in got[0]] == ["a"]          # 오래된 명령은 넘기지 않는다
    svc.send_status(force=True)
    st = json.loads(last_tree(gh)[1]["status.json"])
    assert st["remote_macro"] and st["macro_done"] == ["old"]  # 'a' 는 아직 적용 전
    assert st["macro"]["running"] is True and len(st["macro"]["slots"]) == 24
    svc.check_remote()
    assert len(got) == 1                               # 같은 명령은 다시 넘기지 않는다
    svc.set_paused(True)
    svc.macro_applied(["a"])
    assert svc.send_status() is True                   # 상태가 바뀌었으니 바로 다시 올린다
    st = json.loads(last_tree(gh)[1]["status.json"])
    assert st["macro_done"] == ["old", "a"] and st["macro"]["running"] is False
    assert make(gh, tmp_path)._done_macro == ["a", "old"]  # 다시 켜도 같은 명령은 처리하지 않는다


def test_ops_ignored_when_not_allowed(tmp_path):
    gh = FakeGitHub(request={"macro": [op("a", "running", value=False)]})
    svc = make(gh, tmp_path, remote_macro=False)
    got = []
    svc.on_macro_ops = got.append
    svc.check_remote()
    assert got == [] and svc.send_status() is False


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def tray(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import apphelper.app as appmod
    from apphelper.core.model import Store
    monkeypatch.setattr(appmod, "Store", lambda: Store(tmp_path / "data"))
    t = appmod.TrayApp(app)
    t.data.new_profile("야간")
    t.data.switch_profile("기본")
    t._on_editor_save(t.data)
    yield t
    t.service.stop()
    if t.editor:
        t.editor.close()


def apply(t, *ops):
    acked = []
    t.service.macro_applied = acked.extend
    t.apply_macro_ops(list(ops))
    return acked


def test_app_applies_ops_without_editor(tray):
    acked = apply(tray, op("1", "slot", profile="기본", hour=9, on=True, s=40, e=99),
                  op("2", "slot", profile="다른", hour=10, on=True),   # 다른 프로필 기준이면 무시
                  op("3", "running", value=False))
    assert acked == ["1", "2", "3"]
    s9 = tray.data.slots[9]
    assert (s9.enabled, s9.minute_start, s9.minute_end) == (True, 40, 59) and not tray.data.slots[10].enabled
    assert tray.service.paused and tray.store.load().slots[9].enabled   # 저장까지
    assert tray.service.data.slots[9].enabled                           # 스케줄러에도 반영
    apply(tray, op("4", "profile", name="야간"), op("5", "running", value=True))
    assert tray.data.profile == "야간" and tray.service.data.profile == "야간" and not tray.service.paused


def test_app_applies_ops_with_editor_open_and_toggle_button(tray):
    tray._skip_lock_once = True
    tray.open_editor()
    ed = tray.editor
    assert ed.run_btn.text() == "매크로 ON"
    ed.run_btn.click()                                   # 편집 화면에서 끄기
    assert tray.service.paused and ed.run_btn.text() == "매크로 OFF"
    apply(tray, op("1", "slot", profile="기본", hour=3, on=True, s=5, e=6), op("2", "running", value=True))
    assert ed.data.slots[3].enabled and ed.rows[3].slot.minute_end == 6
    assert tray.service.data.slots[3].enabled and not tray.service.paused and ed.run_btn.text() == "매크로 ON"
    apply(tray, op("3", "profile", name="야간"))
    assert ed.data.profile == "야간" and tray.service.data.profile == "야간"
