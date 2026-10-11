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

    def fetch_results(self) -> dict[str, Any] | None:
        """gh-pages 의 현재 results.json 을 읽는다. 없으면 None."""
        try:
            info = self._call("GET", f"/contents/{RESULTS_FILE}?ref={BRANCH}")
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        content = base64.b64decode(info.get("content", "")).decode("utf-8")
        return json.loads(content) if content else None

    def publish(self, results: dict[str, Any]) -> str:
        """페이지와 결과를 부모 없는 커밋 1개로 만들어 gh-pages 를 덮어쓴다. 커밋 sha 반환."""
        files = {
            "index.html": self.page_html,
            RESULTS_FILE: json.dumps(results, ensure_ascii=False, separators=(",", ":")),
            ".nojekyll": "",
        }
        tree = self._call("POST", "/git/trees", {
            "tree": [
                {"path": path, "mode": "100644", "type": "blob", "content": content}
                for path, content in files.items()
            ]
        })
        commit = self._call("POST", "/git/commits", {
            "message": NEUTRAL_MESSAGE,
            "tree": tree["sha"],
            "parents": [],
            "author": NEUTRAL_AUTHOR,
            "committer": NEUTRAL_AUTHOR,
        })
        sha = commit["sha"]
        try:
            self._call("PATCH", f"/git/refs/heads/{BRANCH}", {"sha": sha, "force": True})
        except HttpError as e:
            if e.status not in (404, 422):
                raise
            # 브랜치가 아직 없으면 새로 만든다.
            self._call("POST", "/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": sha})
        log.debug("gh-pages 갱신: %s", sha)
        return sha


def make_publisher(settings: Any, page_html: str,
                   transport: Transport = urllib_transport) -> Publisher | None:
    if not settings.github_ready:
        return None
    return Publisher(settings.github_owner, settings.github_repo, settings.github_token,
                     page_html, transport)
