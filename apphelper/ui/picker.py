"""좌표 따기: 화면 전체를 덮는 반투명 창을 띄우고 클릭한 위치를 돌려준다."""
from __future__ import annotations

import sys

from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import QApplication, QWidget

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
                   "crash 할 위치를 클릭하세요  (Esc: 취소)")

    def mousePressEvent(self, e):
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


def pick_coordinate(backend: ScreenBackend, hide: list[QWidget] | None = None) -> tuple[int, int] | None:
    """화면을 클릭해 좌표를 지정한다. hide 에 준 창들은 고르는 동안 숨긴다."""
    hidden = [w for w in (hide or []) if w.isVisible()]
    for w in hidden:
        w.hide()
    QApplication.processEvents()
    loop = QEventLoop()
    overlay = _Overlay(backend, loop)
    QTimer.singleShot(150, lambda: (overlay.showFullScreen(), overlay.activateWindow()))
    loop.exec()
    for w in hidden:
        w.show()
    return overlay.result
