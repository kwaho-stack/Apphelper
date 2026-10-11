import base64
import json
from datetime import date, datetime

import pytest

from apphelper.core.model import AppData
from apphelper.core.publisher import HttpError, Publisher
from apphelper.core.results import Results
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
    def __init__(self, branch_exists=True, remote=None):
        self.calls = []
        self.branch_exists = branch_exists
        self.remote = remote

    def __call__(self, method, url, token, body):
        assert token == "tok"
        self.calls.append((method, url, body))
        if method == "GET":
            if self.remote is None:
                raise HttpError(404, "Not Found")
            return {"content": base64.b64encode(json.dumps(self.remote).encode()).decode()}
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
    d.settings.github_owner, d.settings.github_repo, d.settings.github_token = "o", "r", "tok"
    gh = FakeGitHub(remote={"days": {"2026-10-01": {"3": {"r": "작동", "m": 1}},
                                     "2026-10-10": {"3": {"r": "작동", "m": 1}}}})
    svc = Service(d, FakeScreen(), "<html>", transport=gh, now=lambda: datetime(2026, 10, 11, 9, 0))
    assert svc.publish_now() == "commit1"
    tree = {e["path"]: e["content"] for e in gh.calls[1][2]["tree"]}
    data = json.loads(tree["results.json"])
    assert data == {"days": {"2026-10-10": {"3": {"r": "작동", "m": 1}},
                             "2026-10-11": {"14": {"r": "대기"}}}}
    assert "tok" not in tree["results.json"] and "tok" not in tree["index.html"]


def test_service_no_github_settings_noop():
    svc = Service(AppData(), FakeScreen(), "")
    assert svc.publish_now() is None
