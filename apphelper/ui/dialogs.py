"""기본동작 / 핵심 crash / 설정 편집 대화상자."""
from __future__ import annotations

import copy
import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.crash import ScreenBackend, is_on_screen
from ..core.model import BasicAction, KeyCrash, Settings, Step
from .picker import pick_coordinate

COORD_MAX = 99999


def spin(lo: int, hi: int, value: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    if suffix:
        s.setSuffix(suffix)
    return s


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
        form.addRow("기준 시간대", self.base_hour)
        form.addRow("기준 좌표", coord)
        form.addRow("시간당 변동", delta)
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

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(group)
        lay.addWidget(box)

        for w in (self.base_hour, self.base_x, self.base_y, self.dx, self.dy):
            w.valueChanged.connect(self._refresh)
        self._refresh()

    def _collect(self) -> KeyCrash:
        kc = copy.deepcopy(self.kc)
        kc.name = self.name.text().strip()
        kc.base_hour = self.base_hour.value()
        kc.base_x, kc.base_y = self.base_x.value(), self.base_y.value()
        kc.dx, kc.dy = self.dx.value(), self.dy.value()
        kc.wait_ms = self.wait_ms.value()
        kc.region = self.region.value()
        kc.tolerance = self.tolerance.value()
        return kc

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

    def _accept(self) -> None:
        kc = self._collect()
        if not kc.name:
            QMessageBox.warning(self, "Apphelper", "이름을 입력하세요.")
            return
        self.kc = kc
        self.accept()


# ------------------------------------------------------------------ 설정

class _Relay(QObject):
    """작업 스레드의 결과를 GUI 스레드로 넘기는 신호."""
    done = Signal(str)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, on_test: Callable[[Settings], str],
                 on_wipe: Callable[[], None], parent: QWidget | None = None):
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
        self._relay = _Relay(self)
        self._relay.done.connect(self._test_done)
        wipe = QPushButton("전체 데이터 삭제")
        wipe.setStyleSheet("color: #b91c1c;")
        wipe.clicked.connect(self._wipe)
        row = QHBoxLayout()
        row.addWidget(test)
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

        def work():
            try:
                msg = self._on_test(settings)
            except Exception as e:  # noqa: BLE001
                msg = f"실패: {e}"
            try:
                self._relay.done.emit(msg)
            except RuntimeError:  # 그 사이 설정 창이 닫힘
                pass

        threading.Thread(target=work, daemon=True, name="apphelper-test-publish").start()

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
