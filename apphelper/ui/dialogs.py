"""기본동작 / 핵심 crash / 설정 편집 대화상자."""
from __future__ import annotations

import copy
import queue
import threading
import time
import weakref
from datetime import datetime
from typing import Callable

import shiboken6
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.crash import ScreenBackend, is_on_screen, key_crash
from ..core.model import BasicAction, KeyCrash, Settings, Step
from ..core.runner import resolve_target
from .picker import pick_coordinate

COORD_MAX = 99999


def spin(lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


def run_in_background(fn: Callable[[], str], on_done: Callable[[str], None]) -> None:
    """fn 을 작업 스레드에서 돌리고, 결과를 GUI 스레드에서 on_done(창의 메서드)으로 넘긴다.

    - 작업 스레드는 Qt 객체를 전혀 만지지 않는다 (결과는 파이썬 큐로만 전달).
    - 창은 약한 참조로만 잡는다. 그 사이 창이 닫혀 사라지면 결과는 버린다.
      (강한 참조를 쥐면 창의 마지막 참조가 엉뚱한 때 풀리며 Qt 객체가 이중 삭제될 수 있다.)
    """
    results: queue.SimpleQueue[str] = queue.SimpleQueue()
    threading.Thread(target=lambda: results.put(fn()), daemon=True, name="apphelper-worker").start()
    callback = weakref.WeakMethod(on_done)

    def poll() -> None:
        cb = callback()
        if cb is None or not shiboken6.isValid(cb.__self__):
            return
        try:
            msg = results.get_nowait()
        except queue.Empty:
            QTimer.singleShot(50, poll)
            return
        cb(msg)

    QTimer.singleShot(50, poll)


# ------------------------------------------------------------------ 기본동작

class BasicActionDialog(QDialog):
    """기본동작 편집: crash(x, y) 와 대기(ms) 단계만 다룬다."""

    COL_KIND, COL_X, COL_Y, COL_MS, COL_PICK = range(5)

    def __init__(self, action: BasicAction, backend: ScreenBackend, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("기본동작 편집")
        self.resize(560, 420)
        self.backend = backend
        self.action = copy.deepcopy(action)

        self.name = QLineEdit(self.action.name)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["종류", "X", "Y", "대기", ""])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)

        add_pick = QPushButton("+ crash (좌표 따기)")
        add_crash = QPushButton("+ crash")
        add_wait = QPushButton("+ 대기")
        up, down, delete = QPushButton("▲"), QPushButton("▼"), QPushButton("삭제")
        add_pick.clicked.connect(self._add_crash_pick)
        add_crash.clicked.connect(lambda: self._append(Step("crash")))
        add_wait.clicked.connect(lambda: self._append(Step("wait", ms=500)))
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        delete.clicked.connect(self._delete)

        buttons = QHBoxLayout()
        for b in (add_pick, add_crash, add_wait):
            buttons.addWidget(b)
        buttons.addStretch()
        for b in (up, down, delete):
            buttons.addWidget(b)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("이름", self.name)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.table)
        lay.addLayout(buttons)
        lay.addWidget(box)

        for step in self.action.steps:
            self._append(step, select=False)

    def _append(self, step: Step, select: bool = True) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._fill_row(row, step)
        if select:
            self.table.selectRow(row)

    def _fill_row(self, row: int, step: Step) -> None:
        kind = QTableWidgetItem("crash" if step.kind == "crash" else "대기")
        kind.setFlags(kind.flags() & ~Qt.ItemFlag.ItemIsEditable)
        kind.setData(Qt.ItemDataRole.UserRole, step.kind)
        self.table.setItem(row, self.COL_KIND, kind)
        for col in (self.COL_X, self.COL_Y, self.COL_MS, self.COL_PICK):
            self.table.removeCellWidget(row, col)
            blank = QTableWidgetItem("")
            blank.setFlags(Qt.ItemFlag.NoItemFlags)
            self.table.setItem(row, col, blank)
        if step.kind == "crash":
            self.table.setCellWidget(row, self.COL_X, spin(0, COORD_MAX, step.x))
            self.table.setCellWidget(row, self.COL_Y, spin(0, COORD_MAX, step.y))
            pick = QPushButton("좌표 따기")
            pick.clicked.connect(lambda _=False, b=pick: self._pick_for(b))
            self.table.setCellWidget(row, self.COL_PICK, pick)
        else:
            self.table.setCellWidget(row, self.COL_MS, spin(0, 3_600_000, step.ms, " ms"))

    def _row_of(self, widget: QWidget) -> int:
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, self.COL_PICK) is widget:
                return r
        return -1

    def _pick_for(self, button: QWidget) -> None:
        row = self._row_of(button)
        pt = pick_coordinate(self.backend, [self, self.parentWidget()])
        if row >= 0 and pt:
            self.table.cellWidget(row, self.COL_X).setValue(pt[0])
            self.table.cellWidget(row, self.COL_Y).setValue(pt[1])

    def _add_crash_pick(self) -> None:
        pt = pick_coordinate(self.backend, [self, self.parentWidget()])
        if pt:
            self._append(Step("crash", x=pt[0], y=pt[1]))

    def _step_at(self, row: int) -> Step:
        kind = self.table.item(row, self.COL_KIND).data(Qt.ItemDataRole.UserRole)
        if kind == "crash":
            return Step("crash", x=self.table.cellWidget(row, self.COL_X).value(),
                        y=self.table.cellWidget(row, self.COL_Y).value())
        return Step("wait", ms=self.table.cellWidget(row, self.COL_MS).value())

    def steps(self) -> list[Step]:
        return [self._step_at(r) for r in range(self.table.rowCount())]

    def _move(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.table.rowCount():
            return
        a, b = self._step_at(row), self._step_at(target)
        self._fill_row(row, b)
        self._fill_row(target, a)
        self.table.selectRow(target)

    def _delete(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _accept(self) -> None:
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Apphelper", "이름을 입력하세요.")
            return
        self.action.name = name
        self.action.steps = self.steps()
        self.accept()


# ------------------------------------------------------------------ 핵심 crash

class KeyCrashDialog(QDialog):
    def __init__(self, kc: KeyCrash, backend: ScreenBackend, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("핵심 crash 편집")
        self.resize(520, 640)
        self.backend = backend
        self.kc = copy.deepcopy(kc)

        self.name = QLineEdit(kc.name)
        self.base_hour = spin(0, 23, kc.base_hour, "시")
        self.base_x = spin(0, COORD_MAX, kc.base_x)
        self.base_y = spin(0, COORD_MAX, kc.base_y)
        self.dx = spin(-COORD_MAX, COORD_MAX, kc.dx)
        self.dy = spin(-COORD_MAX, COORD_MAX, kc.dy)
        self.wait_ms = spin(0, 600_000, kc.wait_ms, " ms")
        self.region = spin(1, 101, kc.region, " px")
        self.tolerance = spin(0, 255, kc.tolerance)
        pick = QPushButton("좌표 따기")
        pick.clicked.connect(self._pick)

        self.mode = QComboBox()
        self.mode.addItem("좌표로 crash", "coord")
        self.mode.addItem("이미지를 찾아 그 중심을 crash", "image")
        self.mode.setCurrentIndex(1 if kc.uses_image else 0)
        self._image = None
        if kc.image:
            from ..core.imagefind import decode_template
            self._image = decode_template(kc.image)
        self.image_view = QLabel()
        self.image_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_view.setMinimumHeight(90)
        self.image_view.setStyleSheet("border: 1px dashed gray;")
        self.match = spin(50, 100, kc.match, " %")
        cap = QPushButton("화면에서 캡처")
        cap.clicked.connect(self._capture)
        load = QPushButton("파일에서 불러오기")
        load.clicked.connect(self._load_file)
        img_buttons = QHBoxLayout()
        img_buttons.addWidget(cap)
        img_buttons.addWidget(load)
        img_buttons.addStretch()
        img_buttons.addWidget(QLabel("일치도 기준"))
        img_buttons.addWidget(self.match)
        self.image_group = QGroupBox("찾을 이미지 (화면 전체에서 가장 비슷한 곳의 중심을 crash)")
        igl = QVBoxLayout(self.image_group)
        igl.addWidget(self.image_view)
        igl.addLayout(img_buttons)
        self._show_image()

        coord = QHBoxLayout()
        coord.addWidget(QLabel("X"))
        coord.addWidget(self.base_x)
        coord.addWidget(QLabel("Y"))
        coord.addWidget(self.base_y)
        coord.addWidget(pick)
        delta = QHBoxLayout()
        delta.addWidget(QLabel("dx"))
        delta.addWidget(self.dx)
        delta.addWidget(QLabel("dy"))
        delta.addWidget(self.dy)

        form = QFormLayout()
        form.addRow("이름", self.name)
        form.addRow("찾는 방법", self.mode)
        self.coord_form = QWidget()
        cf = QFormLayout(self.coord_form)
        cf.setContentsMargins(0, 0, 0, 0)
        cf.addRow("기준 시간대", self.base_hour)
        cf.addRow("기준 좌표", coord)
        cf.addRow("시간당 변동", delta)
        form.addRow(self.coord_form)
        form.addRow(self.image_group)
        form.addRow("비교 대기시간", self.wait_ms)
        form.addRow("영역 크기 (N×N)", self.region)
        form.addRow("허용오차 (±)", self.tolerance)

        self.preview = QTableWidget(24, 3)
        self.preview.setHorizontalHeaderLabels(["시간대", "좌표", "상태"])
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        group = QGroupBox("시간대별 좌표 미리보기")
        gl = QVBoxLayout(group)
        gl.addWidget(self.preview)

        self.test_btn = QPushButton("지금 테스트 (실제로 한 번 crash)")
        self.test_btn.clicked.connect(self._test)
        self._moved: list[tuple[QWidget, QPoint]] = []

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(group)
        self.preview_group = group
        lay.addWidget(self.test_btn)
        lay.addWidget(box)

        for w in (self.base_hour, self.base_x, self.base_y, self.dx, self.dy):
            w.valueChanged.connect(self._refresh)
        self.mode.currentIndexChanged.connect(self._on_mode)
        self._refresh()
        self._on_mode()

    def _collect(self) -> KeyCrash:
        kc = copy.deepcopy(self.kc)
        kc.name = self.name.text().strip()
        kc.base_hour = self.base_hour.value()
        kc.base_x, kc.base_y = self.base_x.value(), self.base_y.value()
        kc.dx, kc.dy = self.dx.value(), self.dy.value()
        kc.wait_ms = self.wait_ms.value()
        kc.region = self.region.value()
        kc.tolerance = self.tolerance.value()
        kc.mode = self.mode.currentData()
        kc.match = self.match.value()
        if self._image is not None:
            from ..core.imagefind import encode_template
            kc.image = encode_template(self._image)
        return kc

    # 이미지 모드
    def _on_mode(self) -> None:
        image = self.mode.currentData() == "image"
        self.coord_form.setVisible(not image)
        self.preview_group.setVisible(not image)
        self.image_group.setVisible(image)

    def _show_image(self) -> None:
        if self._image is None:
            self.image_view.setText("이미지 없음 — '화면에서 캡처' 또는 '파일에서 불러오기'")
            return
        from .picker import rgb_to_qimage
        pix = QPixmap.fromImage(rgb_to_qimage(self._image))
        if pix.width() > 360 or pix.height() > 160:
            pix = pix.scaled(360, 160, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        self.image_view.setPixmap(pix)
        h, w = self._image.shape[:2]
        self.image_view.setToolTip(f"{w}×{h} px")

    def _set_image(self, rgb) -> None:
        from ..core.imagefind import validate_template
        problem = validate_template(rgb)
        if problem:
            QMessageBox.warning(self, "이미지", problem)
            return
        self._image = rgb
        self._show_image()

    def _capture(self) -> None:
        from .picker import capture_region
        rgb = capture_region(self.backend, [self, self.parentWidget()])
        if rgb is not None:
            self._set_image(rgb)

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "이미지 불러오기", "",
                                              "이미지 (*.png *.jpg *.jpeg *.bmp)")
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "이미지", "이미지를 열 수 없습니다.")
            return
        from .picker import qimage_to_rgb
        self._set_image(qimage_to_rgb(img))

    def _refresh(self) -> None:
        kc = self._collect()
        screen = self.backend.screen_size()
        warn = QBrush(QColor(220, 38, 38))
        for h in range(24):
            x, y = kc.coord_at(h)
            ok = is_on_screen(x, y, screen)
            cells = [f"{h}시" + (" (기준)" if h == kc.base_hour else ""), f"({x}, {y})",
                     "" if ok else "⚠ 화면 밖"]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if not ok:
                    item.setForeground(warn)
                self.preview.setItem(h, c, item)

    def _pick(self) -> None:
        pt = pick_coordinate(self.backend, [self, self.parentWidget()])
        if pt:
            self.base_x.setValue(pt[0])
            self.base_y.setValue(pt[1])

    def _test(self) -> None:
        """현재 시간대 좌표로 핵심 crash 를 한 번 실제로 실행하고 결과를 보여준다 (결과 페이지에는 안 올림)."""
        kc = self._collect()
        hour = datetime.now().hour
        if kc.uses_image and kc.image is None:
            QMessageBox.warning(self, "테스트", "먼저 찾을 이미지를 넣어 주세요.")
            return
        if not kc.uses_image:
            x, y = kc.coord_at(hour)
            if not is_on_screen(x, y, self.backend.screen_size()):
                QMessageBox.warning(self, "테스트", f"{hour}시 좌표 ({x}, {y})가 화면 밖이라 crash 하지 않습니다.")
                return
        self.test_btn.setEnabled(False)
        # 클릭이 이 창에 막히지 않도록 편집 창들을 잠시 화면 밖으로 옮긴다 (숨기면 대화상자가 닫힘).
        self._moved = [(w, w.pos()) for w in (self, self.parentWidget()) if w is not None and w.isVisible()]
        for w, _ in self._moved:
            w.move(-32000, -32000)

        backend = self.backend

        def work() -> str:
            time.sleep(0.4)  # 창이 실제로 비켜날 시간
            try:
                x, y, how = resolve_target(kc, hour, backend)
                out = key_crash(backend, x, y, kc.wait_ms, kc.region, kc.tolerance)
                fmt = lambda c: "(" + ", ".join(f"{v:.0f}" for v in c) + ")"  # noqa: E731
                msg = (f"({x}, {y}) 에 crash 했습니다 [{how}].\n\n"
                       f"클릭 전 평균 색: {fmt(out.before)}\n클릭 후 평균 색: {fmt(out.after)}\n"
                       f"허용오차 ±{kc.tolerance} → 결과: {out.label}")
            except Exception as e:  # noqa: BLE001
                msg = f"테스트 실패: {e}"
            return msg

        run_in_background(work, self._test_done)

    def _test_done(self, msg: str) -> None:
        for w, pos in self._moved:
            w.move(pos)
        self._moved = []
        self.test_btn.setEnabled(True)
        self.raise_()
        self.activateWindow()
        QMessageBox.information(self, "핵심 crash 테스트", msg)

    def _accept(self) -> None:
        kc = self._collect()
        if not kc.name:
            QMessageBox.warning(self, "Apphelper", "이름을 입력하세요.")
            return
        if kc.uses_image and kc.image is None:
            QMessageBox.warning(self, "Apphelper", "찾을 이미지를 넣어 주세요.")
            return
        self.kc = kc
        self.accept()


# ------------------------------------------------------------------ 설정

class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, on_test: Callable[[Settings], str],
                 on_wipe: Callable[[], None], parent: QWidget | None = None,
                 on_change_password: Callable[[QWidget], bool] | None = None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.resize(460, 280)
        self.settings = copy.deepcopy(settings)
        self._on_test = on_test
        self._on_wipe = on_wipe
        self.wiped = False

        self.owner = QLineEdit(settings.github_owner)
        self.repo = QLineEdit(settings.github_repo)
        self.token = QLineEdit(settings.github_token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Contents: Read and write 권한의 fine-grained 토큰")
        self.debug = QCheckBox("디버그 로그 저장 (data/debug.log)")
        self.debug.setChecked(settings.debug_log)

        form = QFormLayout()
        form.addRow("GitHub 소유자", self.owner)
        form.addRow("레포 이름", self.repo)
        form.addRow("토큰", self.token)
        form.addRow("", QLabel("토큰은 이 앱 폴더의 data/config.json 에만 저장됩니다."))
        form.addRow("", self.debug)

        test = self.test_btn = QPushButton("결과 페이지 지금 갱신")
        test.clicked.connect(self._test)
        wipe = QPushButton("전체 데이터 삭제")
        wipe.setStyleSheet("color: #b91c1c;")
        wipe.clicked.connect(self._wipe)
        row = QHBoxLayout()
        row.addWidget(test)
        if on_change_password is not None:
            pw = QPushButton("비밀번호 변경")
            pw.clicked.connect(lambda: on_change_password(self) and
                               QMessageBox.information(self, "비밀번호", "비밀번호를 바꿨습니다."))
            row.addWidget(pw)
        row.addStretch()
        row.addWidget(wipe)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addLayout(row)
        lay.addStretch()
        lay.addWidget(box)

    def _collect(self) -> Settings:
        s = copy.deepcopy(self.settings)
        s.github_owner = self.owner.text().strip()
        s.github_repo = self.repo.text().strip()
        s.github_token = self.token.text().strip()
        s.debug_log = self.debug.isChecked()
        return s

    def _test(self) -> None:
        """네트워크 작업은 별도 스레드에서 돌려 화면이 멈추지 않게 한다."""
        self.test_btn.setEnabled(False)
        self.test_btn.setText("갱신 중…")
        settings = self._collect()

        on_test = self._on_test

        def work() -> str:
            try:
                return on_test(settings)
            except Exception as e:  # noqa: BLE001
                return f"실패: {e}"

        run_in_background(work, self._test_done)

    def _test_done(self, msg: str) -> None:
        self.test_btn.setEnabled(True)
        self.test_btn.setText("결과 페이지 지금 갱신")
        QMessageBox.information(self, "Apphelper", msg)

    def _wipe(self) -> None:
        answer = QMessageBox.question(
            self, "전체 데이터 삭제",
            "로컬 설정, 저장된 동작, 토큰을 모두 지웁니다. 되돌릴 수 없습니다. 계속할까요?")
        if answer == QMessageBox.StandardButton.Yes:
            self._on_wipe()
            self.wiped = True
            self.reject()

    def _accept(self) -> None:
        self.settings = self._collect()
        self.accept()
