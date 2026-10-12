"""좌표 따기: 화면 전체를 덮는 반투명 창을 띄우고 클릭한 위치를 돌려준다."""
from __future__ import annotations

from PySide6.QtCore import QEventLoop, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen
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
            if getattr(self.backend, "has_real_cursor", False):
                # 실제 crash 와 같은 좌표계를 쓰도록 Win32 커서 위치를 읽는다.
                self.result = tuple(self.backend.get_cursor())
            else:  # 더미 백엔드(비 Windows, 테스트): Qt 클릭 위치를 쓴다
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


# ------------------------------------------------------------------ 영역 캡처 (이미지 모드)

def rgb_to_qimage(rgb) -> QImage:
    """(h, w, 3) uint8 배열 → QImage (복사본)."""
    import numpy as np
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


def qimage_to_rgb(img: QImage):
    """QImage → (h, w, 3) uint8 배열."""
    import numpy as np
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return buf[:, : w * 3].reshape(h, w, 3).copy()


class _CaptureOverlay(QWidget):
    """멈춘 화면 위에서 드래그해 영역을 고른다."""

    def __init__(self, shot, loop: QEventLoop):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.shot = shot
        self.image = rgb_to_qimage(shot)
        self.loop = loop
        self.result = None
        self._start: QPoint | None = None
        self._rect = QRect()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(QGuiApplication.primaryScreen().geometry())

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawImage(self.rect(), self.image)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if not self._rect.isNull():
            src = self._to_image(self._rect)
            p.drawImage(self._rect, self.image, src)
            p.setPen(QPen(QColor("#ef4444"), 2))
            p.drawRect(self._rect)
        p.setPen(QColor(255, 255, 255))
        f = p.font()
        f.setPointSize(14)
        p.setFont(f)
        p.drawText(self.rect().adjusted(0, 24, 0, 0), Qt.AlignmentFlag.AlignHCenter,
                   "찾을 부분을 드래그하세요  (Esc 또는 오른쪽 클릭: 취소)")

    def _to_image(self, r: QRect) -> QRect:
        sx = self.image.width() / max(self.width(), 1)
        sy = self.image.height() / max(self.height(), 1)
        return QRect(round(r.x() * sx), round(r.y() * sy), round(r.width() * sx), round(r.height() * sy))

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            self.close()
            return
        self._start = e.position().toPoint()
        self._rect = QRect()

    def mouseMoveEvent(self, e):
        if self._start is not None:
            self._rect = QRect(self._start, e.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._start is None or e.button() != Qt.MouseButton.LeftButton:
            return
        self._start = None
        r = self._to_image(self._rect).intersected(self.image.rect())
        if r.width() >= 2 and r.height() >= 2:
            self.result = self.shot[r.y():r.y() + r.height(), r.x():r.x() + r.width()].copy()
            self.close()
        else:
            self._rect = QRect()
            self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, e):
        self.loop.quit()
        super().closeEvent(e)


def capture_region(backend: ScreenBackend, hide: list[QWidget] | None = None):
    """화면을 멈춘 채로 영역을 드래그해 (h, w, 3) RGB 배열로 돌려준다. 취소하면 None.

    캡처한 화면은 메모리에서만 쓰고 파일로 저장하지 않는다.
    """
    from ..core.imagefind import grab_screen

    faded = [(w, w.windowOpacity()) for w in (hide or []) if w is not None and w.isVisible()]
    for w, _ in faded:
        w.setWindowOpacity(0.0)
    try:
        wait = QEventLoop()
        QTimer.singleShot(300, wait.quit)  # 편집 창이 실제로 사라질 때까지
        wait.exec()
        shot = grab_screen(backend)
        loop = QEventLoop()
        overlay = _CaptureOverlay(shot, loop)
        overlay.setWindowModality(Qt.WindowModality.ApplicationModal)
        timer = QTimer(overlay)
        timer.setSingleShot(True)
        timer.timeout.connect(overlay.close)
        timer.start(PICK_TIMEOUT_MS * 2)
        overlay.showFullScreen()
        overlay.raise_()
        overlay.activateWindow()
        if overlay.isVisible():
            loop.exec()
        result = overlay.result
        overlay.deleteLater()
        return result
    finally:
        for w, opacity in faded:
            w.setWindowOpacity(opacity)
