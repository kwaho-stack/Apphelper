"""원격 스크린샷: 요청 읽기, 암호화, 업로드."""
import json
from datetime import datetime, timedelta, timezone

from apphelper.core.model import AppData
from apphelper.core.remote import (
    SCREEN_FILE, decrypt, derive_key, encrypt, parse_request,
)
from apphelper.service import Service
from tests.fakes import FakeScreen
from tests.test_results_publisher import FakeGitHub

NOW_UTC = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def request(rid="r1", minutes_ago=1):
    at = (NOW_UTC - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"screenshot": {"id": rid, "at": at}}


def test_encrypt_roundtrip_and_key():
    key = derive_key("tok")
    assert len(key) == 32 and key == derive_key("tok") and key != derive_key("tok2")
    blob = encrypt(b"hello", key)
    assert blob[12:] != b"hello" and decrypt(blob, key) == b"hello"
    assert encrypt(b"hello", key) != blob  # 매번 다른 nonce


def test_parse_request():
    r = parse_request(request("abc"))
    assert r.id == "abc" and r.is_fresh(NOW_UTC)
    assert not parse_request(request("abc", minutes_ago=11)).is_fresh(NOW_UTC)
    for bad in (None, {}, {"screenshot": {}}, {"screenshot": {"id": "", "at": "x"}},
                {"screenshot": {"id": "a", "at": "not a date"}}, {"screenshot": {"id": "a" * 65, "at": "2026-01-01T00:00:00Z"}}):
        assert parse_request(bad) is None


def make_service(gh, remote_on=True, image=(b"JPEGDATA", "image/jpeg")):
    d = AppData()
    s = d.settings
    s.github_owner, s.github_repo, s.github_token, s.remote_screenshot = "o", "r", "tok", remote_on
    taken = []

    def capture():
        taken.append(1)
        return image
    svc = Service(d, FakeScreen(), "<html>", transport=gh, now=lambda: datetime(2026, 11, 13, 21, 0, 5),
                  capture_image=capture)
    return svc, taken


def published(gh):
    """마지막 갱신의 (results.json, {경로: 바이트})."""
    tree = [c for c in gh.calls if c[1].endswith("/git/trees")][-1][2]["tree"]
    results = json.loads(next(e["content"] for e in tree if e["path"] == "results.json"))
    bins = {e["path"]: gh.blobs[e["sha"]] for e in tree if "sha" in e}
    return results, bins


def test_new_request_captures_encrypts_and_publishes():
    gh = FakeGitHub(request=request("r1"))
    svc, taken = make_service(gh)
    assert svc.check_remote(NOW_UTC) is True
    results, bins = published(gh)
    assert results["screen"] == {"id": "r1", "at": "2026-11-13 21:00:05", "type": "image/jpeg"}
    assert results["repo"] == "o/r"
    assert decrypt(bins[SCREEN_FILE], derive_key("tok")) == b"JPEGDATA"
    assert b"JPEGDATA" not in bins[SCREEN_FILE]  # 공개 페이지에는 암호문만

    # 같은 요청은 다시 찍지 않는다
    assert svc.check_remote(NOW_UTC) is False and taken == [1]
    # 이후 일반 갱신에도 마지막 스크린샷이 계속 실린다
    svc.publish_now()
    results, bins = published(gh)
    assert results["screen"]["id"] == "r1" and SCREEN_FILE in bins


def test_stale_or_disabled_or_missing_request_does_nothing():
    gh = FakeGitHub(request=request("old", minutes_ago=30))
    svc, taken = make_service(gh)
    assert svc.check_remote(NOW_UTC) is False and taken == []

    gh = FakeGitHub(request=request("r1"))
    svc, taken = make_service(gh, remote_on=False)
    assert svc.check_remote(NOW_UTC) is False and taken == [] and gh.calls == []

    gh = FakeGitHub(request=None)
    svc, taken = make_service(gh)
    assert svc.check_remote(NOW_UTC) is False and taken == []


def test_screenshot_dropped_after_token_change():
    gh = FakeGitHub(request=request("r1"))
    svc, _ = make_service(gh)
    svc.check_remote(NOW_UTC)
    gh.token = "tok2"
    svc.data.settings.github_token = "tok2"
    svc.publish_now()
    results, bins = published(gh)
    assert "screen" not in results and bins == {}
