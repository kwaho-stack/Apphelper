"""Apphelper 진입점: 시스템 트레이 상주, 편집 화면(매크로·메신저 상태·PC 전원), 서비스 연결."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
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
        self.warning = None
        # 화면 가리기: 전용 비밀번호(data/cover.json 에 검증값만), 가리기 프로세스 제어
        from .cover import CoverController
        from .cover.password import CoverPassword
        self.cover = CoverController()
        self.cover_password = CoverPassword(self.store.data_dir)
        self._skip_lock_once = False

        self.tray = QSystemTrayIcon(make_icon(self.service.paused))
        self.tray.setToolTip("Apphelper")
        menu = QMenu()
        self.toggle_action = QAction(menu)
        self.toggle_action.triggered.connect(self.toggle_running)
        menu.addAction(self.toggle_action)
        self.profile_menu = menu.addMenu("프로필")
        self.profile_menu.aboutToShow.connect(self._build_profile_menu)
        menu.addAction("편집 화면 열기", self.open_editor)
        self.cancel_power_action = menu.addAction("종료/다시시작 예약 취소", self.service.power.cancel)
        menu.aboutToShow.connect(self._update_menu)
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
    def _update_menu(self):
        self.cancel_power_action.setVisible(self.service.power.state()["scheduled_at"] is not None)

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
        self.toggle_action.setText("실행" if paused else "일시정지")
        self.tray.setIcon(make_icon(paused))
        # 트레이 툴팁에는 실행 시각이나 결과 같은 정보를 보이지 않는다 (편집 화면에서만 확인)
        self.tray.setToolTip("Apphelper")

    def _build_profile_menu(self):
        self.profile_menu.clear()
        data = self.editor.data if self.editor else self.data
        for name in data.profile_names():
            act = self.profile_menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(name == data.profile)
            act.triggered.connect(lambda _=False, n=name: self.switch_profile(n))

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

    def toggle_running(self):
        if self.editor:
            self.editor.flush()
        self.service.set_paused(not self.service.paused)
        self.data.settings.running = not self.service.paused
        if self.editor:
            self.editor.data.settings.running = self.data.settings.running
        self._save(self.data)
        self._update_tray()

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
    def start_cover(self) -> None:
        """편집 창을 트레이로 숨기고 화면을 검게 가린다.

        키를 누르거나 클릭한 뒤 5초 안에 검은 화면 비밀번호를 치면 풀리고, 못 치면 입력 비상종료
        (창 닫기 → 메신저 끄기 → PC 잠금 → 가리기 해제). 비상종료는 결과 페이지에 시각과 함께 표시한다.
        """
        if self.cover.active:
            return
        verifier = self.cover_password.verifier()
        if verifier is None:  # 처음이면 검은 화면 비밀번호부터 정한다
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
            self._cover_failed(f"화면 가리기를 시작하지 못했습니다: {e}")

    def _cover_tick(self) -> None:
        from .cover import EXIT_EMERGENCY, EXIT_STOPPED, EXIT_UNLOCKED, EXIT_UNSUPPORTED
        code = self.cover.poll()
        if code is None:
            return
        from .cover import cursor
        cursor.restore()  # 가리기 프로세스가 강제 종료돼 커서를 못 되돌렸을 수도 있으니 앱도 한 번 더
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
                                       on_change_cover_password=self.change_cover_password)
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
