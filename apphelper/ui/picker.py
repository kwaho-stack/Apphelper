"""좌표 따기: 화면 전체를 덮는 반투명 창을 띄우고 클릭한 위치를 돌려준다."""
from __future__ import annotations

import sys

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from ..core.crash import ScreenBackend


class _Overlay(QWidget):
    def __init__(self, backend: ScreenBackend, loop: QEventLoop):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.backend = backend
        self.loop = loop
        self.result: tuple[int, int] | None = None
        self.setWindowOpacity(0.25)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.setGeometry(QGuiApplication.primaryScreen().geometry())

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        p.setPen(QColor(255, 255, 255))
        f = p.font()
        f.setPointSize(16)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                   "crash 할 위치를 클릭하세요  (Esc 또는 오른쪽 클릭: 취소)")

    def mousePressEvent(self, e):
        # 왼쪽 클릭: 좌표 지정, 그 밖의 버튼: 취소
        if e.button() == Qt.MouseButton.LeftButton:
            if sys.platform == "win32":
                # 실제 crash 와 같은 좌표계를 쓰도록 Win32 커서 위치를 읽는다.
                self.result = tuple(self.backend.get_cursor())
            else:
                pt = e.globalPosition().toPoint()
                self.result = (pt.x(), pt.y())
        self.close()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, e):
        self.loop.quit()
        super().closeEvent(e)


PICK_TIMEOUT_MS = 60_000


def pick_coordinate(backend: ScreenBackend, hide: list[QWidget] | None = None) -> tuple[int, int] | None:
    """화면을 클릭해 좌표를 지정한다. hide 에 준 창들은 고르는 동안 투명하게 만든다.

    창을 hide() 하면 모달 대화상자의 exec() 가 끝나 버리므로 숨기지 않고 투명도만 0 으로 바꾼다.
    """
    faded = [(w, w.windowOpacity()) for w in (hide or []) if w is not None and w.isVisible()]
    for w, _ in faded:
        w.setWindowOpacity(0.0)
    loop = QEventLoop()
    overlay = _Overlay(backend, loop)
    # 열려 있는 모달 대화상자에 입력이 막히지 않도록 오버레이도 모달로 띄운다.
    overlay.setWindowModality(Qt.WindowModality.ApplicationModal)
    timer = QTimer(overlay)  # 어떤 경우에도 영원히 멈추지 않게 (오버레이와 함께 삭제됨)
    timer.setSingleShot(True)
    timer.timeout.connect(overlay.close)
    timer.start(PICK_TIMEOUT_MS)
    overlay.showFullScreen()
    overlay.raise_()
    overlay.activateWindow()
    try:
        if overlay.isVisible():
            loop.exec()
    finally:
        for w, opacity in faded:
            w.setWindowOpacity(opacity)
        overlay.deleteLater()
    return overlay.result
