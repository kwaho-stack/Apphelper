"""서비스 통합: 메신저 상태 업로드, 원격 전원 명령, PC 상태(heartbeat)."""
import json
from datetime import datetime

from apphelper.core.messenger import Point
from apphelper.core.remote import decrypt_json, derive_key
from apphelper.core.model import AppData
from apphelper.service import Service
from tests.fakes import FakeScreen
from tests.test_results_publisher import FakeGitHub

NOW = 1_800_000_000.0
NOW_DT = datetime(2026, 11, 24, 18, 0, 0)


def make(gh, tmp_path, remote_power=True, clock=None):
    d = AppData()
    s = d.settings
    s.github_owner, s.github_repo, s.github_token = "o", "r", "tok"
    s.remote_power = remote_power
    svc = Service(d, FakeScreen(100, 60, (255, 255, 255)), "<html>", transport=gh, now=lambda: NOW_DT,
                  messenger_log_dir=tmp_path / "messenger", remote_state_path=tmp_path / "remote.json",
                  clock=clock or (lambda: NOW))
    return svc


def last_tree(gh, branch=None):
    """마지막으로 올린 (브랜치, {경로: 내용}). 암호화 파일(.bin)은 풀어서 JSON 문자열로 돌려준다."""
    refs = [c for c in gh.calls if c[0] in ("PATCH", "POST") and "/git/refs" in c[1]]
    trees = [c for c in gh.calls if c[1].endswith("/git/trees")]
    pairs = list(zip(trees, refs))
    if branch:
        pairs = [p for p in pairs if branch in p[1][1] or p[1][2].get("ref", "").endswith(branch)]
    tree, ref = pairs[-1]
    files = {}
    for e in tree[2]["tree"]:
        if "sha" in e:
            files[e["path"]] = json.dumps(decrypt_json(gh.blobs[e["sha"]], derive_key("tok")))
        else:
            files[e["path"]] = e.get("content")
    if "data.bin" in files:
        files["results.json"] = files["data.bin"]
    if "status.bin" in files:
        files["status.json"] = files["status.bin"]
    return ref[1], files


def power_cmd(cid="c1", action="schedule", at_offset=600, mode="restart", age=0):
    return {"power": {"id": cid, "action": action, "at": (NOW + at_offset) * 1000 if action == "schedule" else None,
                      "mode": mode, "issued_at": (NOW - age) * 1000}}


def test_messenger_state_goes_into_results(tmp_path):
    gh = FakeGitHub()
    svc = make(gh, tmp_path, remote_power=False)
    m = svc.data.messenger
    m.enabled, m.confirm_count, m.sample_radius = True, 1, 1
    m.target = Point("", 10, 10)
    svc.backend.fill(8, 8, 5, 5, (220, 40, 40))
    assert svc.messenger_step() is True
    assert svc.messenger_snapshot()[0]["state"] == "온라인"
    svc.publish_now()
    _, files = last_tree(gh)
    data = json.loads(files["results.json"])
    assert data["messenger"]["current"] == "온라인"
    assert (tmp_path / "messenger" / "2026-11-24.txt").exists()

    m.enabled = False  # 꺼도 마지막 상태는 그대로 보이고, 꺼짐 표시가 붙는다
    svc.publish_now()
    _, files = last_tree(gh)
    data = json.loads(files["results.json"])
    assert data["messenger"]["current"] == "온라인" and data["messenger"]["off"] is True

    m.target = Point("", 0, 0)  # 좌표가 없으면 페이지에서 빠진다
    svc.publish_now()
    _, files = last_tree(gh)
    assert "messenger" not in json.loads(files["results.json"])


def test_remote_power_schedule_and_heartbeat(tmp_path):
    gh = FakeGitHub(request=power_cmd())
    svc = make(gh, tmp_path)
    svc.check_remote()
    assert svc.power.state() == {"scheduled_at": NOW + 600, "mode": "restart"}
    assert json.loads((tmp_path / "remote.json").read_text())["last_power_id"] == "c1"

    assert svc.send_status() is True
    ref, files = last_tree(gh)
    assert "apphelper-status" in ref or "apphelper-status" in json.dumps(gh.calls[-1])
    st = json.loads(files["status.json"])
    assert st["online"] and st["scheduled_at"] == int((NOW + 600) * 1000) and st["scheduled_mode"] == "restart"
    assert st["last_command_id"] == "c1"
    assert svc.send_status() is False  # 바뀐 것 없고 60초 안 지남

    # 같은 명령은 다시 처리하지 않는다 (재부팅 후 새 서비스에서도)
    svc.power.cancel()
    again = make(gh, tmp_path)
    again.check_remote()
    assert again.power.state()["scheduled_at"] is None


def test_remote_power_now_cancel_stale_and_disabled(tmp_path):
    gh = FakeGitHub(request=power_cmd("n1", "shutdown_now"))
    svc = make(gh, tmp_path)
    svc.check_remote()
    assert svc.power.tick() == "fire" and svc.power.take() == "shutdown"

    gh.request = power_cmd("old", "restart_now", age=300)  # 5분 전 명령은 무시
    svc.check_remote()
    assert svc.power.tick() is None and svc._last_power_id == "old"

    gh2 = FakeGitHub(request=power_cmd("x1", "shutdown_now"))
    off = make(gh2, tmp_path / "b", remote_power=False)
    assert off.check_remote() is False and off.power.tick() is None and gh2.calls == []


def test_go_offline_sends_offline_status(tmp_path):
    gh = FakeGitHub()
    svc = make(gh, tmp_path)
    svc.go_offline(timeout=5)
    _, files = last_tree(gh)
    assert json.loads(files["status.json"])["online"] is False
