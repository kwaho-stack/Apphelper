import base64
import json
from datetime import date, datetime

import pytest

from apphelper.core.model import AppData
from apphelper.core.publisher import HttpError, Publisher
from apphelper.core.results import Results
from apphelper.core.remote import decrypt_json, derive_key, encrypt_json
from apphelper.service import Service
from tests.fakes import FakeScreen


def test_results_flow():
    r = Results()
    r.mark_checked(date(2026, 10, 11), [9, 14, 15])
    r.record(datetime(2026, 10, 11, 14, 23), True, ["변함!"])
    r.sweep_missed(datetime(2026, 10, 11, 14, 30))
    days = r.to_json()["days"]["2026-10-11"]
    assert days["9"] == {"r": "실패"}          # 지나간 대기 → 실패
    assert days["14"] == {"r": "작동", "m": 23, "k": ["변함!"]}
    assert days["15"] == {"r": "대기"}
    assert list(days) == ["9", "14", "15"]


def test_results_windows():
    r = Results()
    r.mark_checked(date(2026, 10, 11), {9: (10, 40), 14: (0, 59)})
    r.record(datetime(2026, 10, 11, 14, 23), True, ["변함!"], window=(0, 59))
    r.mark_checked(date(2026, 10, 11), {9: (5, 15), 14: (30, 35)})  # 설정 변경
    r.sweep_missed(datetime(2026, 10, 11, 20, 0))
    days = r.to_json()["days"]["2026-10-11"]
    assert days["9"] == {"r": "실패", "w": [5, 15]}               # 대기였던 건 새 범위, 실패 후에도 유지
    assert days["14"] == {"r": "작동", "m": 23, "k": ["변함!"], "w": [0, 59]}  # 실행된 건 당시 범위
    assert "updated" not in r.to_json()
    assert r.to_json(updated=datetime(2026, 10, 11, 20, 1))["updated"] == "2026-10-11 20:01"


def test_service_execute_records_window():
    d = AppData()
    d.slots[14].minute_start, d.slots[14].minute_end = 12, 34
    svc = Service(d, FakeScreen(), "", now=lambda: datetime(2026, 10, 11, 14, 20))
    svc._execute(d.slots[14], d, datetime(2026, 10, 11, 14, 20))
    assert svc.results.get("2026-10-11", 14)["w"] == [12, 34]


def test_results_uncheck_removes_waiting_only():
    r = Results()
    r.mark_checked(date(2026, 10, 11), [1, 2])
    r.record(datetime(2026, 10, 11, 1, 5), False)
    r.mark_checked(date(2026, 10, 11), [])
    assert r.to_json()["days"]["2026-10-11"] == {"1": {"r": "실패", "m": 5}}


def test_results_prune_7_days():
    r = Results({"days": {"2026-10-04": {}, "2026-10-05": {"1": {"r": "대기"}}, "2026-10-11": {}}})
    r.prune(date(2026, 10, 11))
    assert list(r.to_json()["days"]) == ["2026-10-05", "2026-10-11"]


def test_results_merge_remote_local_wins():
    r = Results()
    r.record(datetime(2026, 10, 11, 1, 5), True)
    r.merge_remote({"days": {"2026-10-11": {"1": {"r": "대기"}, "2": {"r": "작동", "m": 3}}}})
    d = r.to_json()["days"]["2026-10-11"]
    assert d["1"]["r"] == "작동" and d["2"]["m"] == 3


class FakeGitHub:
    def __init__(self, branch_exists=True, remote=None, request=None, token="tok"):
        self.calls = []
        self.branch_exists = branch_exists
        self.remote = remote
        self.request = request  # apphelper-remote 브랜치의 request.json
        self.token = token
        self.blobs = {}
        self.files = {}  # 경로 → 바이트 (암호화 파일 등), 있으면 우선

    def __call__(self, method, url, token, body):
        assert token == self.token
        self.calls.append((method, url, body))
        if method == "GET":
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            raw = self.files.get(path)
            if raw is None:
                doc = {"request.json": self.request, "results.json": self.remote}.get(path)
                raw = json.dumps(doc).encode() if doc is not None else None
            if raw is None:
                raise HttpError(404, "Not Found")
            return {"content": base64.b64encode(raw).decode()}
        if url.endswith("/git/blobs"):
            sha = f"blob{len(self.blobs) + 1}"
            self.blobs[sha] = base64.b64decode(body["content"])
            return {"sha": sha}
        if url.endswith("/git/trees"):
            return {"sha": "tree1"}
        if url.endswith("/git/commits"):
            return {"sha": "commit1"}
        if method == "PATCH":
            if not self.branch_exists:
                raise HttpError(422, "Reference does not exist")
            return {}
        if method == "POST" and url.endswith("/git/refs"):
            return {}
        raise AssertionError(url)


def test_publish_orphan_force():
    gh = FakeGitHub()
    sha = Publisher("o", "r", "tok", "<html>", gh).publish({"days": {}})
    assert sha == "commit1"
    tree = gh.calls[0][2]["tree"]
    assert {e["path"] for e in tree} == {"index.html", "results.json", ".nojekyll"}
    assert "base_tree" not in gh.calls[0][2]
    commit = gh.calls[1][2]
    assert commit["parents"] == [] and commit["message"] == "update"
    assert commit["author"]["name"] == "update"
    method, url, body = gh.calls[2]
    assert method == "PATCH" and url.endswith("/git/refs/heads/gh-pages")
    assert body == {"sha": "commit1", "force": True}


def test_publish_creates_branch_when_missing():
    gh = FakeGitHub(branch_exists=False)
    Publisher("o", "r", "tok", "<html>", gh).publish({"days": {}})
    assert gh.calls[-1][2] == {"ref": "refs/heads/gh-pages", "sha": "commit1"}


def test_publish_propagates_other_errors():
    def t(method, url, token, body):
        raise HttpError(401, "Bad credentials")
    with pytest.raises(HttpError):
        Publisher("o", "r", "tok", "", t).publish({})


def test_service_publish_merges_remote_and_minimizes():
    d = AppData()
    d.slots[14].enabled = True
    d.slots[14].minute_start, d.slots[14].minute_end = 40, 10  # 거꾸로 넣어도 실제 쓰는 범위로
    d.settings.github_owner, d.settings.github_repo, d.settings.github_token = "o", "r", "tok"
    gh = FakeGitHub(remote={"days": {"2026-10-01": {"3": {"r": "작동", "m": 1}},
                                     "2026-10-10": {"3": {"r": "작동", "m": 1}}}})
    svc = Service(d, FakeScreen(), "<html>", transport=gh, now=lambda: datetime(2026, 10, 11, 9, 5))
    assert svc.publish_now() == "commit1"
    tree = {e["path"]: e for e in gh.calls[-3][2]["tree"]}
    assert json.loads(tree["results.json"]["content"]) == {"enc": 1, "repo": "o/r"}  # 공개 파일엔 아무 내용 없음
    data = decrypt_json(gh.blobs[tree["data.bin"]["sha"]], derive_key("tok"))
    assert data == {"updated": "2026-10-11 09:05", "repo": "o/r",
                    "days": {"2026-10-10": {"3": {"r": "작동", "m": 1}},
                             "2026-10-11": {"14": {"r": "대기", "w": [10, 40]}}}}
    assert "tok" not in tree["results.json"]["content"] and "tok" not in tree["index.html"]["content"]


def test_service_no_github_settings_noop():
    svc = Service(AppData(), FakeScreen(), "")
    assert svc.publish_now() is None


def test_service_reads_back_encrypted_results_after_restart():
    """앱을 다시 켜면 gh-pages 의 암호화된 결과를 풀어서 이어 쓴다 (토큰이 바뀌면 새로 시작)."""
    d = AppData()
    d.settings.github_owner, d.settings.github_repo, d.settings.github_token = "o", "r", "tok"
    prev = {"days": {"2026-10-11": {"3": {"r": "작동", "m": 7}}}}
    gh = FakeGitHub()
    gh.files["results.json"] = json.dumps({"enc": 1, "repo": "o/r"}).encode()
    gh.files["data.bin"] = encrypt_json(prev, derive_key("tok"))
    svc = Service(d, FakeScreen(), "<html>", transport=gh, now=lambda: datetime(2026, 10, 11, 9, 5))
    svc.publish_now()
    assert svc.results.get("2026-10-11", 3) == {"r": "작동", "m": 7}

    gh.files["data.bin"] = encrypt_json(prev, derive_key("other-token"))
    svc2 = Service(d, FakeScreen(), "<html>", transport=gh, now=lambda: datetime(2026, 10, 11, 9, 5))
    svc2.publish_now()
    assert svc2.results.get("2026-10-11", 3) is None
