"""스케줄러, 슬롯 실행, 결과 기록, GitHub Pages 갱신을 묶는 서비스 계층 (UI 독립)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .core.crash import ScreenBackend
from .core.model import AppData, Settings, Slot
from .core.publisher import Publisher, Transport, make_publisher, urllib_transport
from .core.results import Results
from .core.runner import run_slot
from .core.scheduler import Scheduler

log = logging.getLogger(__name__)


def load_page_template() -> str:
    """번들된 페이지 템플릿(pages/index.html)을 읽는다."""
    import sys
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return (base / "pages" / "index.html").read_text(encoding="utf-8")


class Service:
    def __init__(
        self,
        data: AppData,
        backend: ScreenBackend,
        page_html: str,
        transport: Transport = urllib_transport,
        now: Callable[[], datetime] = datetime.now,
        shots_dir: Path | None = None,
        capture_image: Callable[[], tuple[bytes, str]] | None = None,
    ):
        self.data = data.clone()
        # 원격 스크린샷: capture_image() -> (이미지 바이트, MIME). 최신 1장을 메모리에만 들고 있다.
        self.capture_image = capture_image
        self._screen: tuple[str, bytes, dict] | None = None  # (만든 토큰, 암호문, 메타)
        self._last_request_id: str | None = None
        self._stop = threading.Event()
        self._remote_thread: threading.Thread | None = None
        self.shots_dir = shots_dir  # None 이면 스크린샷을 남기지 않는다
        self._run_lock = threading.Lock()  # 예약 실행과 시범 작동이 동시에 클릭하지 않게
        self.backend = backend
        self.page_html = page_html
        self.transport = transport
        self.now = now
        self.results = Results()
        self.last_status = ""  # 마지막 슬롯 실행 결과 (편집 화면 표시용)
        self._remote_loaded = False
        self._publish_lock = threading.Lock()
        self._publish_pending = threading.Event()
        self._request_lock = threading.Lock()
        self._worker_running = False
        self.scheduler = Scheduler(self.data, self._execute, on_hour=self._on_hour, now=now)

    # ------------------------------------------------------------ 수명
    REMOTE_POLL_SECONDS = 30

    def start(self) -> None:
        self.scheduler.start()
        if self.capture_image is not None and self._remote_thread is None:
            self._remote_thread = threading.Thread(target=self._remote_loop, daemon=True,
                                                   name="apphelper-remote")
            self._remote_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.scheduler.stop()

    @property
    def paused(self) -> bool:
        return self.scheduler.paused

    def set_paused(self, paused: bool) -> None:
        self.data.settings.running = not paused
        self.scheduler.set_paused(paused)

    def update_data(self, data: AppData) -> None:
        self.data = data.clone()
        self.scheduler.update_data(self.data)
        self.request_publish()

    # ------------------------------------------------------------ 스케줄 콜백
    def _recorder(self, slot: Slot, label: str = ""):
        if self.shots_dir is None:
            return None
        from .screenshots import ShotRecorder  # Qt 이미지 기능은 스크린샷이 필요할 때만
        return ShotRecorder(self.backend, self.shots_dir, slot.hour, slot.debug, label, now=self.now)

    def _run(self, data: AppData, slot: Slot, label: str = ""):
        recorder = self._recorder(slot, label)
        with self._run_lock:
            result = run_slot(data, slot, self.backend, sleep=self.scheduler.sleep,
                              should_stop=lambda: self.scheduler.stopping, observer=recorder)
        return result, (recorder.saved if recorder else [])

    def _execute(self, slot: Slot, data: AppData, when: datetime) -> None:
        log.debug("%d시 슬롯 실행 시작 (%d분)", slot.hour, when.minute)
        result, shots = self._run(data, slot)
        self.results.record(when, result.ok, result.key_results, window=slot.window)
        if result.ok:
            detail = " · ".join(result.key_results) or "완료"
        else:
            detail = f"실패: {result.error}"
        if shots:
            detail += f" (스크린샷 {len(shots)}장)"
        self.last_status = f"{when.month}/{when.day} {when.hour}시 {when.minute}분 — {detail}"
        self.request_publish()

    def trial_run(self, data: AppData, hour: int) -> str:
        """시간대 하나를 지금 바로 한 번 실행한다 (결과 페이지에는 올리지 않음). 결과 설명을 돌려준다."""
        slot = data.slots[hour]
        if not slot.blocks:
            return f"{hour}시에 배치된 블록이 없습니다."
        result, shots = self._run(data, slot, label="시범")
        lines = [f"{hour}시 시범 작동 " + ("완료" if result.ok else "실패")]
        if result.key_results:
            lines.append("핵심 crash 결과: " + " · ".join(result.key_results))
        if not result.ok:
            lines.append(f"이유: {result.error}")
        if shots:
            lines.append(f"스크린샷 {len(shots)}장 저장: {shots[0].parent}")
        return "\n".join(lines)

    def _on_hour(self, now: datetime) -> None:
        self.request_publish()

    # ------------------------------------------------------------ 결과 갱신
    def _publisher(self, settings: Settings | None = None) -> Publisher | None:
        return make_publisher(settings or self.data.settings, self.page_html, self.transport)

    def reset_results(self) -> None:
        """전체 데이터 삭제 시 메모리의 결과도 비운다."""
        with self._publish_lock:
            self.results = Results()
            self._remote_loaded = False

    def build_results(self) -> dict:
        now = self.now()
        self.results.mark_checked(now.date(), self.data.enabled_windows())
        self.results.sweep_missed(now)
        self.results.prune(now.date())
        return self.results.to_json(updated=now)

    def publish_now(self, settings: Settings | None = None) -> str | None:
        """동기 갱신. settings 를 주면 그 GitHub 설정으로 갱신한다(설정 화면의 테스트용). 설정이 없으면 None."""
        publisher = self._publisher(settings)
        if publisher is None:
            return None
        with self._publish_lock:
            if not self._remote_loaded:
                self.results.merge_remote(publisher.fetch_results())
                self._remote_loaded = True
            results = self.build_results()
            settings = settings or self.data.settings
            results["repo"] = f"{settings.github_owner}/{settings.github_repo}"
            binaries = {}
            screen = self._screen
            if screen is not None and screen[0] == settings.github_token:  # 토큰이 바뀌면 못 푸니 뺀다
                from .core.remote import SCREEN_FILE
                binaries[SCREEN_FILE] = screen[1]
                results["screen"] = screen[2]
            return publisher.publish(results, binaries)

    # ------------------------------------------------------------ 원격 스크린샷
    def _remote_loop(self) -> None:
        while not self._stop.is_set():
            settings = self.data.settings
            if settings.remote_screenshot and settings.github_ready:
                try:
                    self.check_remote()
                except Exception as e:  # noqa: BLE001 - 네트워크 오류로 멈추지 않게
                    log.debug("원격 요청 확인 실패: %s", e)
            self._stop.wait(self.REMOTE_POLL_SECONDS)

    def check_remote(self, now_utc: datetime | None = None) -> bool:
        """페이지의 스크린샷 요청을 확인하고, 새 요청이면 찍어서 올린다. 찍었으면 True."""
        from .core.remote import derive_key, encrypt, parse_request
        settings = self.data.settings
        if not (settings.remote_screenshot and self.capture_image is not None):
            return False
        publisher = self._publisher()
        if publisher is None:
            return False
        req = parse_request(publisher.fetch_remote_request())
        if req is None or req.id == self._last_request_id:
            return False
        self._last_request_id = req.id
        if not req.is_fresh(now_utc or datetime.now(timezone.utc)):
            return False
        data, mime = self.capture_image()
        taken = self.now()
        meta = {"id": req.id, "at": f"{taken:%Y-%m-%d %H:%M:%S}", "type": mime}
        self._screen = (settings.github_token, encrypt(data, derive_key(settings.github_token)), meta)
        log.debug("원격 스크린샷 %s (%d bytes)", req.id, len(data))
        self.publish_now()
        return True

    def request_publish(self) -> None:
        """비동기 갱신 요청. 여러 요청이 겹치면 한 번으로 합친다."""
        if not self.data.settings.github_ready:
            return
        with self._request_lock:
            self._publish_pending.set()
            if self._worker_running:
                return
            self._worker_running = True
        threading.Thread(target=self._publish_worker, daemon=True, name="apphelper-publish").start()

    def _publish_worker(self) -> None:
        while True:
            with self._request_lock:
                if not self._publish_pending.is_set():
                    self._worker_running = False
                    return
                self._publish_pending.clear()
            try:
                self.publish_now()
            except Exception as e:  # noqa: BLE001 - 네트워크 오류로 앱이 멈추지 않게 한다
                log.debug("결과 갱신 실패: %s", e)
