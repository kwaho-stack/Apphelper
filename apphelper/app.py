"""Apphelper 진입점: 시스템 트레이 상주, 편집 화면(매크로·메신저 상태·PC 전원), 서비스 연결."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .backend import get_backend
from .core.model import AppData, Settings, Store
from .service import Service, load_page_template
from .core.lock import PasswordLock
from .ui.editor import EditorWindow
from .ui.guards import ask_new_cover_password, ask_new_password, ask_password, install_no_wheel

INSTANCE_NAME = "apphelper-single-instance"
log = logging.getLogger("apphelper")


def login_lines(items: list[dict]) -> str:
    """자동 로그인 진행 상황 → '1. Genian — 로그인 중…' 줄들."""
    icon = {"대기": "⏳", "로그인 중": "▶", "완료": "✅", "실패": "❌"}
    return "\n".join(f"{i + 1}. {it['name']} — {icon.get(it['state'], '')} {it['state']}"
                     + (f" ({it['msg']})" if it.get("msg") else "") for i, it in enumerate(items))


def _window_grabber():
    """메신저 창 직접 캡처 (Windows 에서만)."""
    if sys.platform != "win32":
        return None
    from .wincapture import grab_messenger
    return grab_messenger


def configure_logging(store: Store, enabled: bool) -> None:
    """로컬 로그는 기본 OFF. 디버그 설정을 켰을 때만 data/debug.log 에 남긴다."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    if enabled:
        store.data_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(store.data_dir / "debug.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
    else:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL + 1)


def make_icon(paused: bool) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#9ca3af" if paused else "#2563eb"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    f = QFont()
    f.setPixelSize(38)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "A")
    p.end()
    return QIcon(pix)


class _Bridge(QObject):
    """서비스(다른 스레드) → 화면 스레드로 일을 넘기는 신호."""
    macro_ops = Signal(object)
    login_request = Signal(str)
    cover_request = Signal(str)
    net_relogin = Signal()
    closeall_request = Signal(str)


class TrayApp:
    def __init__(self, app: QApplication):
        self.app = app
        self.store = Store()
        self.lock = PasswordLock(self.store.data_dir)
        self.first_run = not self.store.exists()
        self.data = self.store.load()
        configure_logging(self.store, self.data.settings.debug_log)
        self.backend = get_backend()
        self.page_html = load_page_template()
        self.shots_dir = self.store.data_dir / "screenshots"
        self.messenger_log_dir = self.store.data_dir / "messenger" / "logs"
        from .screenshots import capture_image
        self.service = Service(self.data, self.backend, self.page_html, shots_dir=self.shots_dir,
                               capture_image=lambda: capture_image(self.backend),
                               messenger_log_dir=self.messenger_log_dir,
                               remote_state_path=self.store.data_dir / "remote_state.json",
                               window_grabber=_window_grabber())
        self.editor: EditorWindow | None = None
        # 페이지의 매크로 조정 명령은 서비스 스레드에서 오므로 신호로 화면 스레드에 넘겨 적용한다
        self._bridge = _Bridge()
        self._bridge.macro_ops.connect(self.apply_macro_ops)
        self.service.on_macro_ops = self._bridge.macro_ops.emit
        self._bridge.login_request.connect(self.start_remote_login)
        self.service.on_login_request = self._bridge.login_request.emit
        self._bridge.cover_request.connect(lambda rid: self.start_cover(remote_id=rid))
        self.service.on_cover_request = self._bridge.cover_request.emit
        self._bridge.net_relogin.connect(self.start_net_relogin)
        self._bridge.closeall_request.connect(self.close_all_windows)
        self.service.on_closeall_request = self._bridge.closeall_request.emit
        self.service.on_net_relogin = self._bridge.net_relogin.emit
        import threading
        self._login_lock = threading.Lock()  # 자동 로그인은 한 번에 하나만 (편집 화면·페이지 동시에 눌러도)
        self.warning = None
        # 화면 가리기: 전용 비밀번호(data/cover.json 에 검증값만), 가리기 프로세스 제어
        from .cover import CoverController
        from .cover.password import CoverPassword
        self.cover = CoverController()
        self.cover_password = CoverPassword(self.store.data_dir)
        self._skip_lock_once = False

        self.tray = QSystemTrayIcon(make_icon(self.service.paused))
        self.tray.setToolTip("Apphelper")
        # 트레이 메뉴는 '열기'와 '종료'만. 나머지(매크로 켜기/끄기, 프로필, 예약 취소)는 편집 화면에서 한다.
        menu = QMenu()
        menu.addAction("열기", self.open_editor)
        menu.addSeparator()
        menu.addAction("종료", self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self._update_tray()

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(2000)
        self._power_timer = QTimer()  # 종료/다시시작 예약 확인 (1초)
        self._power_timer.timeout.connect(self._power_tick)
        self._power_timer.start(1000)

        self.server = QLocalServer()
        QLocalServer.removeServer(INSTANCE_NAME)
        self.server.listen(INSTANCE_NAME)
        self.server.newConnection.connect(self._on_other_instance)

    # ------------------------------------------------------------ 수명
    def start(self) -> None:
        from .cover import cursor
        cursor.restore()  # 지난번 가리기 중 비정상 종료로 커서가 숨겨진 채 남았을 경우 대비
        self.tray.show()
        self.service.start()
        self.service.request_publish()
        if self.first_run:
            self.open_editor()

    def quit(self) -> None:
        power = self.service.power
        if power.state()["scheduled_at"] is not None:
            from PySide6.QtWidgets import QMessageBox
            from .core.power import MODE_NAMES
            if QMessageBox.question(None, "Apphelper 종료",
                                    f"{MODE_NAMES[power.state()['mode']]} 예약이 있습니다. "
                                    "Apphelper 를 끝내면 예약이 취소됩니다. 계속할까요?") \
                    != QMessageBox.StandardButton.Yes:
                return
            power.cancel()
        if self.editor:
            self.editor.flush()
        self.service.go_offline()
        self.service.stop()
        self.tray.hide()
        self.app.quit()

    # ------------------------------------------------------------ PC 전원
    def power_now(self, mode: str) -> None:
        self.service.power.now_action(mode)
        self._power_tick()

    def _power_tick(self) -> None:
        self._cover_tick()
        action = self.service.power.tick()
        if action == "warn":
            from .ui.panels import PowerWarning
            self.warning = PowerWarning(self.service.power)
            self.warning.show()
            self.warning.raise_()
            self.warning.activateWindow()
        elif action == "fire":
            mode = self.service.power.take()
            if mode:
                self._do_power(mode)

    def _do_power(self, mode: str) -> None:
        """실제 종료/다시시작. 페이지에 '꺼짐'을 먼저 알리고 Windows 명령을 실행한다."""
        from .core.power import shutdown_now
        if self.warning is not None:
            self.warning.close()
        if self.editor:
            self.editor.flush()
        self.service.go_offline()
        shutdown_now(force=self.data.settings.power_force, restart=mode == "restart")

    # ------------------------------------------------------------ 트레이
    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_editor()

    def _on_other_instance(self):
        conn = self.server.nextPendingConnection()
        if conn:
            conn.close()
        self.open_editor()

    def _update_tray(self):
        paused = self.service.paused
        self.tray.setIcon(make_icon(paused))
        # 트레이 툴팁에는 실행 시각이나 결과 같은 정보를 보이지 않는다 (편집 화면에서만 확인)
        self.tray.setToolTip("Apphelper")

    def switch_profile(self, name: str):
        """트레이에서 프로필 전환. 편집 화면이 있으면 그쪽 데이터를 기준으로 바꾼다."""
        if self.editor:
            self.editor.switch_profile(name)
            self.editor.flush()
        elif name != self.data.profile:
            data = self.data.clone()
            data.switch_profile(name)
            self._on_editor_save(data)
        self._update_tray()

    def _refresh_status(self):
        status = self.service.last_status
        if self.editor:
            self.editor.set_last_run(status)
            self.editor.set_live(self.service.messenger_snapshot())

    def set_running(self, running: bool) -> None:
        if running == (not self.service.paused):
            return
        self.toggle_running()

    def apply_macro_ops(self, ops: list) -> None:
        """결과 페이지에서 온 매크로 조정: 켜기/끄기, 프로필 바꾸기, 시간대 사용·분 범위."""
        data = self.editor.data if self.editor else self.data.clone()
        changed, running = False, None
        for op in ops:
            kind = op.get("op")
            try:
                if kind == "running":
                    running = bool(op.get("value"))
                elif kind == "profile":
                    name = str(op.get("name", ""))
                    if name in data.profile_names() and name != data.profile:
                        data.switch_profile(name)
                        changed = True
                elif kind == "slot":
                    if op.get("profile") not in (None, data.profile):
                        continue  # 페이지가 보던 것과 다른 프로필이면 건드리지 않는다
                    slot = data.slots[int(op["hour"])]
                    if "on" in op:
                        slot.enabled = bool(op["on"])
                    if "s" in op:
                        slot.minute_start = max(0, min(59, int(op["s"])))
                    if "e" in op:
                        slot.minute_end = max(0, min(59, int(op["e"])))
                    changed = True
            except (KeyError, IndexError, TypeError, ValueError) as e:
                log.debug("잘못된 매크로 명령 %s: %s", op, e)
        if changed:
            if self.editor:
                self.editor._rebind()
                self.editor.changed(refresh=False)
                self.editor.flush()
            else:
                self._on_editor_save(data)
        if running is not None:
            self.set_running(running)
        self.service.macro_applied([str(op.get("id")) for op in ops])

    def _login_ops(self):
        if sys.platform != "win32":
            return None
        from .autologin_win import WinLoginOps
        return WinLoginOps(self.backend)

    def run_logins(self, targets: list, progress=None, run_id: str | None = None, by: str = "app") -> str:
        """자동 로그인 (작업 스레드에서 불림). 위에서부터 하나씩: 로그인 중 → 완료/실패.

        진행 상황은 progress(줄 목록) 로 편집 화면에, service.set_login_state 로 결과 페이지에 알린다.
        매크로 클릭과 겹치지 않게 실행 잠금을 잡는다.
        """
        import time as _time
        from datetime import datetime
        from .core.autologin import run_login
        if not self._login_lock.acquire(blocking=False):
            return "이미 자동 로그인을 실행 중입니다."
        try:
            ops = self._login_ops()
            items = [{"name": t.name or f"{i + 1}번", "state": "대기", "msg": ""} for i, t in enumerate(targets)]
            state = {"id": run_id or f"app-{int(_time.time() * 1000)}", "by": by,
                     "started": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "done": False, "items": items}

            def report():
                self.service.set_login_state({**state, "items": [dict(i) for i in items]})
                if progress is not None:
                    progress(login_lines(items))

            report()
            with self.service._run_lock:
                for t, item in zip(targets, items):
                    item["state"] = "로그인 중"
                    report()
                    if ops is None:
                        ok, msg = False, "자동 로그인은 Windows 에서만 됩니다"
                    else:
                        try:
                            r = run_login(t, ops)
                            ok, msg = r.ok, r.message
                        except Exception as e:  # noqa: BLE001 - 한 항목이 실패해도 다음 항목은 한다
                            ok, msg = False, f"오류 {e}"
                    item["state"], item["msg"] = ("완료" if ok else "실패"), msg
                    report()
            state["done"] = True
            report()
            return login_lines(items)
        finally:
            self._login_lock.release()

    def start_remote_login(self, run_id: str, by: str = "page") -> None:
        """결과 페이지의 '전체 자동 로그인'(체크된 항목) 또는 네트워크 끊김 감지(by="net": 그 옵션을 켠 항목).

        화면 스레드에서 불리고, 실제 로그인은 작업 스레드에서 한다.
        """
        import threading
        if self.editor:
            self.editor.flush()
        data = self.editor.data if self.editor else self.data
        pick = (lambda t: t.net_relogin) if by == "net" else (lambda t: t.enabled)
        targets = [type(t).from_dict(t.to_dict()) for t in data.logins if pick(t)]
        if not targets:
            self.service.set_login_state({"id": run_id, "by": by, "done": True, "items": [],
                                          "error": "체크된 자동 로그인 항목이 없습니다"})
            return
        threading.Thread(target=lambda: self.run_logins(targets, run_id=run_id, by=by),
                         daemon=True, name="apphelper-login").start()

    def close_all_windows(self, request_id: str) -> None:
        """결과 페이지의 '모든 창 종료': 화면에 떠 있는 프로그램 창에 모두 닫기(X)를 보낸다.

        Apphelper 자신(편집 화면·가리기)과 바탕 화면·작업 표시줄은 건드리지 않는다.
        """
        import os
        if sys.platform != "win32":
            self.service.set_closeall_state(request_id, None, "Windows 에서만 됩니다")
            return
        from .cover.emergency import close_windows
        keep = {os.getpid()}
        if self.cover.proc is not None:
            keep.add(self.cover.proc.pid)
        try:
            n = close_windows(keep)
        except Exception as e:  # noqa: BLE001
            self.service.set_closeall_state(request_id, None, str(e))
            return
        self.service.set_closeall_state(request_id, n)

    def start_net_relogin(self) -> None:
        """네트워크가 몇 분째 끊김 → '네트워크가 끊기면 자동 로그인' 항목(예: Genian)을 로그인."""
        import time as _time
        self.start_remote_login(f"net-{int(_time.time() * 1000)}", by="net")

    def toggle_running(self):
        if self.editor:
            self.editor.flush()
        self.service.set_paused(not self.service.paused)
        self.data.settings.running = not self.service.paused
        if self.editor:
            self.editor.data.settings.running = self.data.settings.running
        self._save(self.data)
        self._update_tray()
        if self.editor:
            self.editor.set_running(self.data.settings.running)

    # ------------------------------------------------------------ 편집 화면
    def _unlock(self) -> bool:
        """편집 화면을 열기 전 비밀번호 확인. 처음이면 비밀번호를 정하게 한다."""
        if not self.lock.is_set():
            pw = ask_new_password(first=True)
            if pw is None:
                return False
            self.lock.set(pw)
            return True
        retry = False
        for _ in range(3):
            pw = ask_password(retry=retry)
            if pw is None:
                return False
            if self.lock.verify(pw):
                return True
            retry = True
        return False

    def change_password(self, parent=None) -> bool:
        pw = ask_new_password(parent, first=False)
        if pw is None:
            return False
        self.lock.set(pw)
        return True

    def change_cover_password(self, parent=None) -> bool:
        """검은 화면 전용 비밀번호를 정하거나 바꾼다."""
        pw = ask_new_cover_password(parent, first=not self.cover_password.is_set())
        if pw is None:
            return False
        self.cover_password.set(pw)
        return True

    # ------------------------------------------------------------ 화면 가리기
    def start_cover(self, remote_id: str | None = None) -> None:
        """편집 창을 트레이로 숨기고 화면을 검게 가린다.

        키를 누르거나 클릭한 뒤 5초 안에 검은 화면 비밀번호를 치면 풀리고, 못 치면 입력 비상종료
        (창 닫기 → 메신저 끄기 → PC 잠금 → 가리기 해제). 비상종료는 결과 페이지에 시각과 함께 표시한다.
        """
        if self.cover.active:
            if remote_id:
                self.service.set_cover_state(True, request=remote_id)
            return
        verifier = self.cover_password.verifier()
        if verifier is None:  # 처음이면 검은 화면 비밀번호부터 정한다
            if remote_id:  # 페이지에서 요청: PC 에 창을 띄우지 않고 이유만 알린다
                self.service.set_cover_state(False, error="PC 에서 검은 화면 비밀번호를 먼저 정해야 합니다",
                                             request=remote_id)
                return
            if not self.change_cover_password(self.editor):
                return
            verifier = self.cover_password.verifier()
        if self.editor:
            self.editor.flush()
            self.editor.close()
        self.service.clear_emergency()  # 새로 가리면 지난 비상종료 표시는 지운다
        try:
            self.cover.start(verifier)
        except OSError as e:
            self.service.set_cover_state(False, error=f"시작하지 못함: {e}", request=remote_id)
            self._cover_failed(f"화면 가리기를 시작하지 못했습니다: {e}")
            return
        self.service.set_cover_state(True, request=remote_id)

    def _cover_tick(self) -> None:
        from .cover import EXIT_EMERGENCY, EXIT_STOPPED, EXIT_UNLOCKED, EXIT_UNSUPPORTED
        code = self.cover.poll()
        if code is None:
            return
        from .cover import cursor
        cursor.restore()  # 가리기 프로세스가 강제 종료돼 커서를 못 되돌렸을 수도 있으니 앱도 한 번 더
        failed = code not in (EXIT_UNLOCKED, EXIT_STOPPED, EXIT_EMERGENCY)
        self.service.set_cover_state(False, error=f"가리기가 끝남 (코드 {code})" if failed else None)
        if code in (EXIT_UNLOCKED, EXIT_STOPPED):
            return  # 풀린 뒤에는 트레이에만 있는다 (편집 화면은 평소처럼 트레이에서 비밀번호로 연다)
        if code == EXIT_EMERGENCY:
            self.service.record_emergency()  # 창 닫기·메신저 끄기·잠금은 가리기 프로세스가 이미 했다
            return
        if code == EXIT_UNSUPPORTED:
            self._cover_failed("이 Windows 버전에서는 화면 가리기를 쓸 수 없습니다 (Windows 10 2004 이상 필요).")
        else:
            self._cover_failed(f"화면 가리기가 예상치 않게 끝났습니다 (코드 {code}).")

    def _cover_failed(self, msg: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        self._skip_lock_once = True  # 가려지지 않았으니 방금 숨긴 편집 화면을 다시 보여 준다
        self.open_editor()
        QMessageBox.warning(self.editor, "화면 가리기", msg)

    def open_editor(self):
        # 이미 떠 있는 편집 화면을 앞으로 가져오는 것은 그대로, 닫혀 있던 화면을 열 때만 잠금 확인
        if self.cover.active:
            return  # 가린 동안에는 비밀번호로 풀어야 한다
        skip, self._skip_lock_once = self._skip_lock_once, False
        if not (self.editor and self.editor.isVisible()) and not skip and not self._unlock():
            return
        if self.editor is None:
            self.editor = EditorWindow(self.data, self.backend, self._on_editor_save,
                                       self._test_publish, self._wipe, self.change_password,
                                       on_trial_run=self.service.trial_run, shots_dir=self.shots_dir,
                                       power=self.service.power, on_power_now=self.power_now,
                                       messenger_log_dir=self.messenger_log_dir,
                                       autostart=self._autostart(), on_cover=self.start_cover,
                                       on_change_cover_password=self.change_cover_password,
                                       on_toggle_running=self.toggle_running,
                                       on_login=self.run_logins,
                                       on_test_notify=lambda text: self.service.notify(text, wait=True))
        self.editor.show()
        self.editor.setWindowState(self.editor.windowState() & ~Qt.WindowState.WindowMinimized)
        self.editor.raise_()
        self.editor.activateWindow()

    def _on_editor_save(self, data: AppData):
        # 실행/일시정지 상태는 트레이가 주인이다.
        data.settings.running = not self.service.paused
        debug_changed = data.settings.debug_log != self.data.settings.debug_log
        self.data = data
        self._save(data)
        if debug_changed:
            configure_logging(self.store, data.settings.debug_log)
        self.service.update_data(data)
        self._update_tray()

    def _save(self, data: AppData):
        try:
            self.store.save(data)
        except OSError as e:
            log.debug("저장 실패: %s", e)

    def _test_publish(self, settings: Settings) -> str:
        if not settings.github_ready:
            return "GitHub 소유자, 레포 이름, 토큰을 모두 입력하세요."
        sha = self.service.publish_now(settings)
        return f"갱신 완료 (gh-pages {sha[:7]})"

    @staticmethod
    def _autostart():
        from . import autostart
        return (autostart.enabled, autostart.set_enabled) if autostart.supported() else None

    def _wipe(self) -> AppData:
        """전체 데이터 삭제: 로컬 설정, 저장된 동작, 토큰, 메신저 기록, 자동 실행 등록을 지운다."""
        from . import autostart
        configure_logging(self.store, False)  # 로그 파일 핸들을 닫아야 지울 수 있다
        try:
            autostart.set_enabled(False)
        except OSError:
            pass
        self.service.data.messenger.enabled = False  # 기록 파일을 지우는 동안 새로 쓰지 않게
        self.service.monitor = None
        self.store.wipe()
        self.service.reset_results()
        self.data = AppData()
        self.data.settings.running = not self.service.paused
        self.service.update_data(self.data)
        return self.data


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Apphelper")
    # Windows 11 기본 스타일은 스핀박스 화살표가 넓어 숫자가 가려지므로 Fusion 으로 통일한다.
    app.setStyle("Fusion")
    install_no_wheel(app)
    app.setQuitOnLastWindowClosed(False)

    # 이미 실행 중이면 그쪽 편집 화면을 열고 끝낸다.
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_NAME)
    if sock.waitForConnected(300):
        sock.disconnectFromServer()
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return 1
    tray_app = TrayApp(app)
    tray_app.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
