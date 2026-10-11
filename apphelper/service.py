"""스케줄러, 슬롯 실행, 결과 기록, GitHub Pages 갱신을 묶는 서비스 계층 (UI 독립)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
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
    ):
        self.data = data.clone()
        self.backend = backend
        self.page_html = page_html
        self.transport = transport
        self.now = now
        self.results = Results()
        self._remote_loaded = False
        self._publish_lock = threading.Lock()
        self._publish_pending = threading.Event()
        self._request_lock = threading.Lock()
        self._worker_running = False
        self.scheduler = Scheduler(self.data, self._execute, on_hour=self._on_hour, now=now)

    # ------------------------------------------------------------ 수명
    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
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
    def _execute(self, slot: Slot, data: AppData, when: datetime) -> None:
        log.debug("%d시 슬롯 실행 시작 (%d분)", slot.hour, when.minute)
        result = run_slot(data, slot, self.backend, should_stop=lambda: self.scheduler.stopping)
        self.results.record(when, result.ok, result.key_results)
        self.request_publish()

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
        self.results.mark_checked(now.date(), self.data.enabled_hours())
        self.results.sweep_missed(now)
        self.results.prune(now.date())
        return self.results.to_json()

    def publish_now(self, settings: Settings | None = None) -> str | None:
        """동기 갱신. settings 를 주면 그 GitHub 설정으로 갱신한다(설정 화면의 테스트용). 설정이 없으면 None."""
        publisher = self._publisher(settings)
        if publisher is None:
            return None
        with self._publish_lock:
            if not self._remote_loaded:
                self.results.merge_remote(publisher.fetch_results())
                self._remote_loaded = True
            return publisher.publish(self.build_results())

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
