"""Apphelper 진입점: 시스템 트레이 상주, 편집 화면, 스케줄러 서비스 연결."""
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
from .ui.guards import ask_new_password, ask_password, install_no_wheel

INSTANCE_NAME = "apphelper-single-instance"
log = logging.getLogger("apphelper")


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
        self.service = Service(self.data, self.backend, self.page_html)
        self.editor: EditorWindow | None = None

        self.tray = QSystemTrayIcon(make_icon(self.service.paused))
        self.tray.setToolTip("Apphelper")
        menu = QMenu()
        self.toggle_action = QAction(menu)
        self.toggle_action.triggered.connect(self.toggle_running)
        menu.addAction(self.toggle_action)
        self.profile_menu = menu.addMenu("프로필")
        self.profile_menu.aboutToShow.connect(self._build_profile_menu)
        menu.addAction("편집 화면 열기", self.open_editor)
        menu.addSeparator()
        menu.addAction("종료", self.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self._update_tray()

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(2000)

        self.server = QLocalServer()
        QLocalServer.removeServer(INSTANCE_NAME)
        self.server.listen(INSTANCE_NAME)
        self.server.newConnection.connect(self._on_other_instance)

    # ------------------------------------------------------------ 수명
    def start(self) -> None:
        self.tray.show()
        self.service.start()
        self.service.request_publish()
        if self.first_run:
            self.open_editor()

    def quit(self) -> None:
        if self.editor:
            self.editor.flush()
        self.service.stop()
        self.tray.hide()
        self.app.quit()

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
        profile = (self.editor.data if self.editor else self.data).profile
        tip = f"Apphelper — {'일시정지' if paused else '실행 중'} [{profile}]"
        if self.service.last_status:
            tip += f"\n마지막 실행: {self.service.last_status}"
        self.tray.setToolTip(tip)

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
        if status != getattr(self, "_shown_status", None):
            self._shown_status = status
            self._update_tray()
        if self.editor:
            self.editor.set_last_run(status)

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

    def open_editor(self):
        # 이미 떠 있는 편집 화면을 앞으로 가져오는 것은 그대로, 닫혀 있던 화면을 열 때만 잠금 확인
        if not (self.editor and self.editor.isVisible()) and not self._unlock():
            return
        if self.editor is None:
            self.editor = EditorWindow(self.data, self.backend, self._on_editor_save,
                                       self._test_publish, self._wipe, self.change_password)
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

    def _wipe(self) -> AppData:
        """전체 데이터 삭제: 로컬 설정, 저장된 동작, 토큰을 지우고 빈 상태로 돌아간다."""
        configure_logging(self.store, False)  # 로그 파일 핸들을 닫아야 지울 수 있다
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
