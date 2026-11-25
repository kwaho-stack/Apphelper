"""스케줄러, 슬롯 실행, 결과 기록, GitHub Pages 갱신, 메신저 상태 감시, 원격 요청(스크린샷·전원)을
묶는 서비스 계층 (UI 독립)."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .core.crash import ScreenBackend
from .core.messenger import MessengerLog, MessengerMonitor
from .core.model import AppData, Settings, Slot
from .core.power import PowerScheduler, parse_command
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
        messenger_log_dir: Path | None = None,
        remote_state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.data = data.clone()
        self.clock = clock
        # 메신저 상태 감시 (설정에서 켜고 좌표가 있을 때만 돈다)
        self.messenger_log = MessengerLog(messenger_log_dir) if messenger_log_dir else None
        self.monitor: MessengerMonitor | None = None
        self._monitor_lock = threading.Lock()
        self._messenger_thread: threading.Thread | None = None
        # PC 종료/다시시작 예약 (GUI 와 원격이 함께 쓴다)
        self.power = PowerScheduler(clock=clock)
        self.remote_state_path = remote_state_path
        state = self._load_remote_state()
        self._last_power_id = state.get("last_power_id")
        # 화면 가리기 입력 비상종료 시각 ("YYYY-MM-DD HH:MM:SS"). 다음 가리기를 시작할 때 지운다.
        self.emergency_at: str | None = state.get("emergency_at")
        self._status_version = -1
        self._status_sent_at = 0.0
        self._last_publish = -1e9
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
    REMOTE_POLL_SECONDS = 5       # 원격 요청(전원·스크린샷) 확인 주기
    HEARTBEAT_SECONDS = 60        # PC 켜짐 상태 갱신 주기
    MIN_PUBLISH_INTERVAL = 30     # 결과 페이지 갱신 최소 간격 (Pages 빌드가 너무 잦지 않게)

    def start(self) -> None:
        self.scheduler.start()
        if self._remote_thread is None:
            self._remote_thread = threading.Thread(target=self._remote_loop, daemon=True,
                                                   name="apphelper-remote")
            self._remote_thread.start()
        if self.messenger_log is not None and self._messenger_thread is None:
            self._messenger_thread = threading.Thread(target=self._messenger_loop, daemon=True,
                                                      name="apphelper-messenger")
            self._messenger_thread.start()

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

    def _run(self, data: AppData, slot: Slot, label: str = "",
             cancel: Callable[[], bool] | None = None):
        """cancel() 이 True 가 되면 다음 단계에서 멈춘다 (대기 중이면 0.05초 안에)."""
        recorder = self._recorder(slot, label)

        def stopped() -> bool:
            return self.scheduler.stopping or (cancel is not None and cancel())

        def sleep(seconds: float) -> None:
            if cancel is None:
                self.scheduler.sleep(seconds)
                return
            end = time.monotonic() + max(seconds, 0)
            while not stopped() and time.monotonic() < end:
                self.scheduler.sleep(min(0.05, end - time.monotonic()))

        with self._run_lock:
            result = run_slot(data, slot, self.backend, sleep=sleep, should_stop=stopped, observer=recorder)
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
        # 시범 작동은 Esc 로 취소할 수 있다 (예약 실행은 다른 프로그램에서 Esc 를 눌러도 멈추지 않게 제외)
        esc = getattr(self.backend, "escape_pressed", None)
        cancelled = False
        if esc is not None:
            esc()  # 시작 전에 눌렸던 기록은 버린다

            def cancel() -> bool:
                nonlocal cancelled
                cancelled = cancelled or bool(esc())
                return cancelled
        else:
            cancel = None
        result, shots = self._run(data, slot, label="시범", cancel=cancel)
        if cancelled:
            msg = f"{hour}시 시범 작동을 Esc 로 취소했습니다."
            if shots:
                msg += f"\n스크린샷 {len(shots)}장 저장: {shots[0].parent}"
            return msg
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
            if self.emergency_at:
                results["emergency"] = self.emergency_at
            messenger = self.messenger_web_data()
            if messenger is not None:
                results["messenger"] = messenger
            from .core.remote import DATA_FILE, SCREEN_FILE, encrypt_json, envelope
            binaries = {}
            screen = self._screen
            if screen is not None and screen[0] == settings.github_token:  # 토큰이 바뀌면 못 푸니 뺀다
                binaries[SCREEN_FILE] = screen[1]
                results["screen"] = screen[2]
            # 공개 페이지에는 '암호화됨' 표시만, 결과 전체는 토큰으로 암호화해서 올린다
            binaries[DATA_FILE] = encrypt_json(results, publisher.key)
            return publisher.publish(envelope(results["repo"]), binaries)

    # ------------------------------------------------------------ 메신저 상태
    def _ensure_monitor(self) -> MessengerMonitor | None:
        cfg = self.data.messenger
        if self.messenger_log is None or not (cfg.enabled and cfg.ready):
            self.monitor = None
            return None
        with self._monitor_lock:
            if self.monitor is None or not self.monitor.same_layout(cfg):
                self.monitor = MessengerMonitor(cfg, self.messenger_log, self.now())
            else:
                self.monitor.config = cfg  # 확인 주기·허용오차 같은 값은 그대로 반영
            return self.monitor

    def messenger_step(self) -> bool:
        """메신저 상태를 한 번 확인한다. 바뀐 게 있으면 결과 페이지 갱신을 요청하고 True."""
        monitor = self._ensure_monitor()
        if monitor is None:
            return False
        with self._monitor_lock:
            changed, _ = monitor.step(self.backend, self.now())
        if changed:
            self.request_publish()
        return changed

    def _messenger_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.messenger_step()
            except Exception as e:  # noqa: BLE001 - 화면 읽기 실패로 멈추지 않게
                log.debug("메신저 상태 확인 실패: %s", e)
            self._stop.wait(max(1, self.data.messenger.poll_interval))

    def messenger_snapshot(self) -> list[dict[str, str]]:
        monitor = self.monitor
        return monitor.snapshot() if monitor else []

    def messenger_web_data(self) -> dict | None:
        """페이지용 메신저 데이터. 감시가 꺼져 있어도 좌표가 있으면 기록에 남은 마지막 상태를 보여 주고 off 로 표시."""
        cfg = self.data.messenger
        if self.messenger_log is None or not cfg.ready:
            return None
        monitor = self._ensure_monitor()
        if monitor is None:  # 감시 꺼짐: 기록 파일만 읽어 마지막 상태를 만든다
            data = MessengerMonitor(cfg, self.messenger_log, self.now()).web_data(self.now())
            data["off"] = True
            return data
        with self._monitor_lock:
            return monitor.web_data(self.now())

    # ------------------------------------------------------------ 화면 가리기 비상종료
    def record_emergency(self, when: datetime | None = None) -> None:
        """검은 화면에서 비밀번호 없이 입력이 들어와 비상종료했음을 남기고 페이지에 바로 알린다."""
        self.emergency_at = f"{when or self.now():%Y-%m-%d %H:%M:%S}"
        self._save_remote_state()
        self._last_publish = -1e9  # 최소 간격을 기다리지 않고 바로 올린다
        self.request_publish()

    def clear_emergency(self) -> None:
        if self.emergency_at is None:
            return
        self.emergency_at = None
        self._save_remote_state()
        self.request_publish()

    # ------------------------------------------------------------ 원격 요청 (스크린샷·전원)
    def _load_remote_state(self) -> dict:
        try:
            with open(self.remote_state_path, encoding="utf-8") as f:  # type: ignore[arg-type]
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return {}

    def _save_remote_state(self) -> None:
        if self.remote_state_path is None:
            return
        try:
            self.remote_state_path.parent.mkdir(parents=True, exist_ok=True)
            self.remote_state_path.write_text(json.dumps({"last_power_id": self._last_power_id,
                                                          "emergency_at": self.emergency_at}),
                                              encoding="utf-8")
        except OSError as e:
            log.debug("원격 상태 저장 실패: %s", e)

    def _remote_enabled(self) -> bool:
        s = self.data.settings
        return s.github_ready and (s.remote_power or (s.remote_screenshot and self.capture_image is not None))

    def _remote_loop(self) -> None:
        while not self._stop.is_set():
            if self._remote_enabled():
                try:
                    self.check_remote()
                except Exception as e:  # noqa: BLE001 - 네트워크 오류로 멈추지 않게
                    log.debug("원격 요청 확인 실패: %s", e)
                try:
                    self.send_status()
                except Exception as e:  # noqa: BLE001
                    log.debug("PC 상태 갱신 실패: %s", e)
            self._stop.wait(self.REMOTE_POLL_SECONDS)

    def check_remote(self, now_utc: datetime | None = None) -> bool:
        """페이지가 보낸 요청(request.json)을 확인해 처리한다. 스크린샷을 찍었으면 True."""
        if not self._remote_enabled():
            return False
        publisher = self._publisher()
        if publisher is None:
            return False
        raw = publisher.fetch_remote_request() or {}
        self._handle_power(raw.get("power"))
        return self._handle_screenshot(raw, now_utc)

    def _handle_power(self, cmd) -> None:
        if not isinstance(cmd, dict) or not cmd.get("id") or cmd["id"] == self._last_power_id:
            return
        # 다시시작 뒤 같은 명령을 또 실행하지 않도록, 실행 전에 처리한 id 를 파일로 남긴다
        self._last_power_id = str(cmd["id"])
        self._save_remote_state()
        parsed = parse_command(cmd, self.clock())
        if parsed and self.data.settings.remote_power:
            log.debug("원격 전원 명령: %s", parsed)
            self.power.apply_remote(parsed)
        self._status_version = -1  # 받았다는 걸 바로 알린다

    def _handle_screenshot(self, raw: dict, now_utc: datetime | None) -> bool:
        from .core.remote import derive_key, encrypt, parse_request
        settings = self.data.settings
        if not (settings.remote_screenshot and self.capture_image is not None):
            return False
        req = parse_request(raw)
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

    def status_dict(self, online: bool = True) -> dict:
        st = self.power.state()
        at = st["scheduled_at"]
        return {
            "online": online,
            "updated_at": int(self.clock() * 1000),
            "scheduled_at": int(at * 1000) if at else None,
            "scheduled_mode": st["mode"] if at else None,
            "last_command_id": self._last_power_id,
            "remote_power": self.data.settings.remote_power,
        }

    def send_status(self, force: bool = False, online: bool = True) -> bool:
        """PC 상태(켜짐·예약)를 apphelper-status 브랜치에 올린다. 바뀌었거나 60초가 지났을 때만."""
        from .core.remote import STATUS_BIN, STATUS_BRANCH, encrypt_json
        if not self.data.settings.remote_power:
            return False
        publisher = self._publisher()
        if publisher is None:
            return False
        now = self.clock()
        version = self.power.version
        if not force and version == self._status_version and now - self._status_sent_at < self.HEARTBEAT_SECONDS:
            return False
        publisher.write_branch(STATUS_BRANCH, {},
                               {STATUS_BIN: encrypt_json(self.status_dict(online), publisher.key)})
        self._status_version, self._status_sent_at = version, now
        return True

    def go_offline(self, timeout: float = 6) -> None:
        """앱 종료·PC 종료 직전에 '꺼짐'으로 알린다 (최선 노력, timeout 초 안에)."""
        if not (self.data.settings.remote_power and self.data.settings.github_ready):
            return
        t = threading.Thread(target=lambda: self._try(lambda: self.send_status(force=True, online=False)),
                             daemon=True)
        t.start()
        t.join(timeout)

    @staticmethod
    def _try(fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log.debug("실패: %s", e)

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
            wait = self._last_publish + self.MIN_PUBLISH_INTERVAL - time.monotonic()
            if wait > 0 and self._stop.wait(wait):
                with self._request_lock:
                    self._worker_running = False
                return
            self._last_publish = time.monotonic()
            try:
                self.publish_now()
            except Exception as e:  # noqa: BLE001 - 네트워크 오류로 앱이 멈추지 않게 한다
                log.debug("결과 갱신 실패: %s", e)
