"""GitHub Pages 연동: gh-pages 브랜치를 부모 없는 커밋 1개로 덮어쓴다.

- 결과는 gh-pages 브랜치(orphan)에만 올린다.
- 갱신할 때마다 parents=[] 커밋을 새로 만들고 ref 를 force=true 로 교체하므로
  브랜치에는 항상 커밋 1개만 남는다.
- 커밋 메시지와 작성자명은 고정된 중립 값을 쓴다.
- 토큰은 로컬 설정에서만 받아 요청 헤더에만 쓴다.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol

log = logging.getLogger(__name__)

# (owner, repo, branch, path) → (커밋 sha, 내용). 커밋이 바뀌지 않았으면 다시 받지 않는다.
_FILE_CACHE: dict[tuple[str, str, str, str], tuple[str, bytes | None]] = {}

API = "https://api.github.com"
BRANCH = "gh-pages"
RESULTS_FILE = "results.json"
NEUTRAL_MESSAGE = "update"
NEUTRAL_AUTHOR = {"name": "update", "email": "update@localhost", "date": "2000-01-01T00:00:00Z"}


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


class Transport(Protocol):
    def __call__(self, method: str, url: str, token: str, body: dict[str, Any] | None) -> Any: ...


def urllib_transport(method: str, url: str, token: str, body: dict[str, Any] | None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace")) from None
    return json.loads(raw) if raw else None


class Publisher:
    def __init__(self, owner: str, repo: str, token: str,
                 page_html: str, transport: Transport = urllib_transport):
        if not (owner and repo and token):
            raise ValueError("GitHub owner/repo/token 이 필요합니다")
        self.owner, self.repo, self._token = owner, repo, token
        self.page_html = page_html
        self._t = transport

    def _url(self, path: str) -> str:
        return f"{API}/repos/{self.owner}/{self.repo}{path}"

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._t(method, self._url(path), self._token, body)

    def _ref_sha(self, branch: str) -> str | None:
        """브랜치의 최신 커밋 sha (없으면 None). ref 는 바로 반영된다."""
        try:
            info = self._call("GET", f"/git/ref/heads/{branch}")
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        return info["object"]["sha"]

    def _get_bytes(self, path: str, branch: str) -> bytes | None:
        """브랜치 최신 커밋에서 파일 내용을 읽는다. 없으면 None.

        contents API 는 GitHub CDN 에 수십 초~1분 캐시되어 방금 올린 내용을 늦게 보여 주므로 쓰지 않는다.
        브랜치 ref → 커밋 → 트리 → blob 순서로 읽고, 커밋이 그대로면 지난번 내용을 다시 쓴다
        (평소에는 ref 조회 1번으로 끝난다).
        """
        sha = self._ref_sha(branch)
        if sha is None:
            return None
        key = (self.owner, self.repo, branch, path)
        cached = _FILE_CACHE.get(key)
        if cached is not None and cached[0] == sha:
            return cached[1]
        commit = self._call("GET", f"/git/commits/{sha}")
        tree = self._call("GET", f"/git/trees/{commit['tree']['sha']}")
        entry = next((e for e in tree.get("tree", []) if e.get("path") == path), None)
        data = None
        if entry is not None:
            blob = self._call("GET", f"/git/blobs/{entry['sha']}")
            data = base64.b64decode(blob.get("content", "") or "")
        _FILE_CACHE[key] = (sha, data)
        return data

    @property
    def key(self) -> bytes:
        from .remote import derive_key
        return derive_key(self._token)

    def fetch_results(self) -> dict[str, Any] | None:
        """gh-pages 의 현재 결과. 암호화된 data.bin 을 토큰으로 풀고, 예전 평문 results.json 도 읽는다.

        없거나 풀 수 없으면(토큰이 바뀜) None.
        """
        from .remote import DATA_FILE, decrypt_json
        raw = self._get_bytes(RESULTS_FILE, BRANCH)
        if not raw:
            return None
        doc = json.loads(raw.decode("utf-8"))
        if not doc.get("enc"):
            return doc  # 예전 평문 형식
        blob = self._get_bytes(DATA_FILE, BRANCH)
        if not blob:
            return None
        try:
            return decrypt_json(blob, self.key)
        except ValueError:
            log.debug("이전 결과를 풀 수 없음 (토큰이 바뀜) — 새로 시작")
            return None

    def fetch_remote_request(self) -> dict[str, Any] | None:
        """페이지가 apphelper-remote 브랜치에 남긴 요청 (암호화된 request.bin, 예전 평문 request.json)."""
        from .remote import REMOTE_BRANCH, REQUEST_BIN, REQUEST_FILE, decrypt_json
        blob = self._get_bytes(REQUEST_BIN, REMOTE_BRANCH)
        if blob is not None:
            try:
                return decrypt_json(blob, self.key)
            except ValueError:
                log.debug("요청을 풀 수 없음 (토큰이 다름)")
                return None
        raw = self._get_bytes(REQUEST_FILE, REMOTE_BRANCH)
        return json.loads(raw.decode("utf-8")) if raw else None

    def publish(self, results: dict[str, Any], binaries: dict[str, bytes] | None = None) -> str:
        """페이지와 결과(그리고 binaries 의 파일들)를 부모 없는 커밋 1개로 만들어 gh-pages 를 덮어쓴다.

        커밋 sha 반환.
        """
        files = {
            "index.html": self.page_html,
            RESULTS_FILE: json.dumps(results, ensure_ascii=False, separators=(",", ":")),
            ".nojekyll": "",
        }
        return self.write_branch(BRANCH, files, binaries)

    def write_branch(self, branch: str, files: dict[str, str],
                     binaries: dict[str, bytes] | None = None) -> str:
        """branch 를 files(+binaries) 만 담은 부모 없는 커밋 1개로 덮어쓴다 (이력이 남지 않음)."""
        entries: list[dict[str, Any]] = [
            {"path": path, "mode": "100644", "type": "blob", "content": content}
            for path, content in files.items()
        ]
        for path, data in (binaries or {}).items():
            blob = self._call("POST", "/git/blobs", {
                "content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._call("POST", "/git/trees", {"tree": entries})
        commit = self._call("POST", "/git/commits", {
            "message": NEUTRAL_MESSAGE,
            "tree": tree["sha"],
            "parents": [],
            "author": NEUTRAL_AUTHOR,
            "committer": NEUTRAL_AUTHOR,
        })
        sha = commit["sha"]
        try:
            self._call("PATCH", f"/git/refs/heads/{branch}", {"sha": sha, "force": True})
        except HttpError as e:
            if e.status not in (404, 422):
                raise
            # 브랜치가 아직 없으면 새로 만든다.
            self._call("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        log.debug("%s 갱신: %s", branch, sha)
        return sha


def make_publisher(settings: Any, page_html: str,
                   transport: Transport = urllib_transport) -> Publisher | None:
    if not settings.github_ready:
        return None
    return Publisher(settings.github_owner, settings.github_repo, settings.github_token,
                     page_html, transport)
