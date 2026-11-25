"""검은 화면: 5초 안에 비밀번호 / 비상종료 판단, 검은 화면 비밀번호 저장, 비상종료 대상 고르기."""
import json
from datetime import datetime

import pytest

from apphelper.cover.emergency import should_close, should_kill
from apphelper.cover.keys import Gate, check, make_verifier
from apphelper.cover.password import CoverPassword, problem

VK = {c: ord(c.upper()) for c in "abcd"}


def type_keys(gate, text):
    out = []
    for c in text:
        out += gate.key(VK[c], True) + gate.key(VK[c], False)
    return out


def test_first_key_arms_and_password_within_time_unlocks():
    g = Gate(make_verifier("abcd"))
    assert type_keys(g, "ab") == ["arm"]      # 첫 키에서 5초 시계 시작, 한 번만
    assert type_keys(g, "cd") == ["unlock"]
    assert g.done == "unlock" and g.expire() is False


def test_click_arms_and_timeout_is_emergency():
    g = Gate(make_verifier("abcd"))
    assert g.click() == ["arm"] and g.click() == []
    assert type_keys(g, "abc") == []
    assert g.expire() is True and g.done == "emergency"
    assert type_keys(g, "d") == []            # 비상종료가 시작되면 늦게 친 비밀번호는 소용없다


def test_wrong_keys_then_password_still_unlocks_in_time():
    g = Gate(make_verifier("abcd"))
    assert type_keys(g, "ddab") == ["arm"]
    assert type_keys(g, "cd") == ["unlock"]


def test_cover_password_file(tmp_path):
    cp = CoverPassword(tmp_path)
    assert not cp.is_set() and cp.verifier() is None
    cp.set("Pass-12")
    raw = json.loads((tmp_path / "cover.json").read_text())
    assert "Pass-12" not in json.dumps(raw)
    assert check(cp.verifier(), "Pass-12") and not check(cp.verifier(), "Pass-13")
    with pytest.raises(ValueError):
        cp.set("한글비번1")
    assert problem("abc") and problem("한글비번1") and problem("Ok-123") is None


def test_emergency_targets():
    assert should_kill("AtMessenger.exe") and should_kill("ATMESSENGER32.EXE")
    assert not should_kill("explorer.exe") and not should_kill("chrome.exe")
    keep = {100, 200}
    ok = dict(cls="Chrome_WidgetWin_1", title="문서", visible=True, owned=False, tool=False, cloaked=False,
              pid=5, keep_pids=keep)
    assert should_close(**ok)
    assert not should_close(**{**ok, "visible": False})
    assert not should_close(**{**ok, "title": ""})
    assert not should_close(**{**ok, "owned": True})         # 대화상자 등은 본 창과 함께 닫힌다
    assert not should_close(**{**ok, "tool": True})
    assert not should_close(**{**ok, "cloaked": True})
    assert not should_close(**{**ok, "pid": 200})            # Apphelper 자신
    for cls in ("Progman", "Shell_TrayWnd", "WorkerW", "ApphelperCover"):
        assert not should_close(**{**ok, "cls": cls})       # 바탕 화면·작업 표시줄은 건드리지 않는다


def test_service_puts_emergency_in_results_and_keeps_it_after_restart(tmp_path):
    from apphelper.core.model import AppData
    from apphelper.core.remote import decrypt_json, derive_key
    from apphelper.service import Service
    from tests.fakes import FakeScreen
    from tests.test_results_publisher import FakeGitHub
    d = AppData()
    d.settings.github_owner, d.settings.github_repo, d.settings.github_token = "o", "r", "tok"
    gh = FakeGitHub()
    now = datetime(2026, 11, 25, 14, 3, 22)
    mk = lambda: Service(d, FakeScreen(), "<html>", transport=gh, now=lambda: now,  # noqa: E731
                         remote_state_path=tmp_path / "remote.json")
    svc = mk()
    svc.request_publish = lambda: None
    svc.record_emergency()
    svc.publish_now()
    tree = {e["path"]: e for e in gh.calls[-3][2]["tree"]}
    assert decrypt_json(gh.blobs[tree["data.bin"]["sha"]], derive_key("tok"))["emergency"] == "2026-11-25 14:03:22"
    svc2 = mk()                                # 앱을 다시 켜도 표시는 남는다
    assert svc2.emergency_at == "2026-11-25 14:03:22"
    svc2.request_publish = lambda: None
    svc2.clear_emergency()
    assert "emergency" not in svc2.build_results() and mk().emergency_at is None

