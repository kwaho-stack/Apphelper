"""실행 중 스크린샷 저장 (data/screenshots/날짜/ 아래 PNG).

- 핵심 crash 결과가 '안변함!' 이면 그 순간 화면을 저장한다.
- 시간대에 상세 디버그를 켜면 매 동작마다 저장한다.
클릭한 위치는 빨간 원으로 표시한다. QImage/QPainter 는 작업 스레드에서도 쓸 수 있다.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen

from .core.crash import UNCHANGED, ScreenBackend

log = logging.getLogger(__name__)

EVENT_NAMES = {"crash": "crash", "key_before": "핵심crash_전", "key_after": "핵심crash_후"}


def _safe(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_")[:40]


def save_screenshot(backend: ScreenBackend, path: Path, mark: tuple[int, int] | None, caption: str) -> Path:
    from .core.imagefind import grab_screen
    from .ui.picker import rgb_to_qimage

    img = rgb_to_qimage(grab_screen(backend)).convertToFormat(QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if mark is not None:
        p.setPen(QPen(QColor("#ef4444"), 3))
        p.drawEllipse(QPoint(*mark), 18, 18)
        p.drawLine(mark[0] - 26, mark[1], mark[0] - 8, mark[1])
        p.drawLine(mark[0] + 8, mark[1], mark[0] + 26, mark[1])
    if QGuiApplication.instance() is not None:  # 글자 그리기는 Qt 앱이 있어야 한다
        f = QFont()
        f.setPixelSize(16)
        p.setFont(f)
        p.fillRect(0, 0, min(img.width(), 16 + 9 * len(caption)), 26, QColor(0, 0, 0, 170))
        p.setPen(QColor("white"))
        p.drawText(8, 19, caption)
    p.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not img.save(str(path), "PNG"):
        raise OSError(f"스크린샷 저장 실패: {path}")
    return path


class ShotRecorder:
    """한 번의 슬롯 실행 동안 runner 의 observer 로 쓰인다."""

    def __init__(self, backend: ScreenBackend, base_dir: Path, hour: int, debug: bool,
                 label: str = "", now=datetime.now):
        self.backend = backend
        self.base_dir = Path(base_dir)
        self.hour = hour
        self.debug = debug
        self.label = label  # 예: "시범" — 파일 이름에 붙는다
        self.now = now
        self.saved: list[Path] = []
        self._n = 0
        self._last_mark: tuple[int, int] | None = None

    def _path(self, kind: str, note: str) -> Path:
        t = self.now()
        self._n += 1
        tag = f"_{self.label}" if self.label else ""
        name = f"{t:%H-%M-%S}_{self.hour}시{tag}_{self._n:02d}_{kind}"
        if note:
            name += f"_{_safe(note)}"
        return self.base_dir / f"{t:%Y-%m-%d}" / f"{name}.png"

    def __call__(self, event: str, x: int, y: int, note: str) -> None:
        unchanged = event == "key_after" and note == UNCHANGED
        if not (self.debug or unchanged):
            return
        if self.debug:
            kind, name_note = EVENT_NAMES.get(event, event), note
        else:  # 안변함 스크린샷
            kind, name_note = "안변함", ""
        caption = f"{self.now():%Y-%m-%d %H:%M:%S}  {self.hour}시  {EVENT_NAMES.get(event, event)} ({x}, {y})  {note}"
        try:
            self.saved.append(save_screenshot(self.backend, self._path(kind, name_note), (x, y), caption))
        except Exception as e:  # noqa: BLE001 - 스크린샷 실패가 실행을 막지 않게
            log.debug("스크린샷 실패: %s", e)
