"""편집 화면의 '메신저 상태' / 'PC 전원' 탭 (기존 mess, closet 기능)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QTime, QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTimeEdit, QVBoxLayout, QWidget,
)

from ..core.crash import average_color
from ..core.messenger import (
    KOR, Anchor, MessengerLog, Point, WindowFrame, classify, import_mess_folder, rebase,
)
from ..core.power import MODE_NAMES, PowerScheduler
from .picker import pick_coordinate

if TYPE_CHECKING:
    from .editor import EditorWindow

COORD_MAX = 99999
STATE_COLORS = {"온라인": "#dc2626", "자리비움": "#2563eb", "오프라인": "#6b7280", "확인불가": "#a16207"}


def _spin(lo: int, hi: int, suffix: str = "") -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    if suffix:
        s.setSuffix(suffix)
    return s


def _fmt_remaining(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ------------------------------------------------------------------ 메신저 상태

def window_at(x: int, y: int):
    """화면 (x, y) 에 있는 창 (WindowRef). Windows 가 아니면 None. 테스트에서 바꿔 끼운다."""
    import sys
    if sys.platform != "win32":
        return None
    from ..wincapture import window_at as f
    return f(x, y)


def grab_messenger(ref):
    """(그림, 창 위치) 또는 실패 이유. 테스트에서 바꿔 끼운다."""
    import sys
    if sys.platform != "win32":
        return "Windows 에서만 쓸 수 있습니다"
    from ..wincapture import grab_messenger as f
    return f(ref)


class MessengerPanel(QWidget):
    """메신저 상태 감시 설정. 바꾸면 editor.data.messenger 에 바로 반영되고 자동 저장된다."""

    def __init__(self, editor: "EditorWindow", log_dir: Path | None):
        super().__init__()
        self.editor = editor
        self.log_dir = log_dir
        self._loading = False

        self.enabled = QCheckBox("메신저 상태 감시 사용")
        self.enabled.toggled.connect(self._apply)
        self._capture_error = ""

        # 가려져도 읽기 (메신저 창 직접 캡처)
        self.capture = QCheckBox("가려져도 읽기 (메신저 창을 직접 캡처 — 다른 창이 덮어도 됨, 최소화는 안 됨)")
        self.capture.setToolTip("화면 대신 메신저 창의 그림을 직접 읽습니다. 메신저 창을 옮겨도 좌표를 다시 딸 필요가 없습니다.\n"
                                "켤 때 주 대상 좌표에 있는 창을 메신저 창으로 기억합니다 (그때는 메신저가 보여야 함).")
        self.capture.toggled.connect(self._toggle_capture)
        cap_test = QPushButton("메신저 창 캡처 시험")
        cap_test.setToolTip("메신저 창을 다른 창으로 덮어 둔 채 눌러 보세요. 직접 찍은 메신저 창 그림을 보여 줍니다.")
        cap_test.clicked.connect(self._test_capture)
        crow = QHBoxLayout()
        crow.addWidget(self.capture, 1)
        crow.addWidget(cap_test)

        # 주 대상
        self.name = QLineEdit()
        self.name.setPlaceholderText("이름 (페이지 표시용, 비워도 됨)")
        self.tx, self.ty = _spin(0, COORD_MAX), _spin(0, COORD_MAX)
        pick_t = QPushButton("좌표 따기")
        pick_t.clicked.connect(self._pick_target)
        self.target_now = QLabel()
        trow = QHBoxLayout()
        for w in (QLabel("X"), self.tx, QLabel("Y"), self.ty, pick_t):
            trow.addWidget(w)
        trow.addStretch()
        tbox = QGroupBox("주 대상 (상태 아이콘 정중앙)")
        tf = QFormLayout(tbox)
        tf.addRow("이름", self.name)
        tf.addRow("좌표", trow)
        tf.addRow("지금 읽은 상태", self.target_now)

        # 기준점
        self.ax, self.ay = _spin(0, COORD_MAX), _spin(0, COORD_MAX)
        self.anchor_rgb = QLabel()
        pick_a = QPushButton("좌표 따기")
        pick_a.clicked.connect(self._pick_anchor)
        self.tolerance = _spin(0, 255)
        arow = QHBoxLayout()
        for w in (QLabel("X"), self.ax, QLabel("Y"), self.ay, pick_a):
            arow.addWidget(w)
        arow.addStretch()
        abox = QGroupBox("기준점 (이름 목록 아래 '흰 빈 공간' — 로그인 화면·창 가림 감지)")
        af = QFormLayout(abox)
        af.addRow("좌표", arow)
        af.addRow("등록된 색", self.anchor_rgb)
        af.addRow("허용오차 (±)", self.tolerance)

        # 추가 인원
        self.others = QTableWidget(0, 4)
        self.others.setHorizontalHeaderLabels(["이름", "X", "Y", ""])
        self.others.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.others.verticalHeader().setVisible(False)
        self.others.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.others.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.others.setMinimumHeight(110)
        add = QPushButton("+ 사람 추가 (좌표 따기)")
        add.clicked.connect(self._add_other)
        delete = QPushButton("선택 삭제")
        delete.clicked.connect(self._delete_other)
        obox = QGroupBox("현황만 볼 사람 (인원 제한 없음)")
        ol = QVBoxLayout(obox)
        ol.addWidget(self.others)
        orow = QHBoxLayout()
        orow.addWidget(add)
        orow.addWidget(delete)
        orow.addStretch()
        ol.addLayout(orow)

        # 세부 설정
        self.interval = _spin(1, 3600, " 초")
        self.confirm = _spin(1, 100, " 회")
        self.rollover = _spin(0, 23, " 시")
        self.radius = _spin(0, 20, " px")
        dbox = QGroupBox("세부 설정")
        df = QFormLayout(dbox)
        df.addRow("확인 주기", self.interval)
        df.addRow("연속 확인 횟수 (깜빡임 방지)", self.confirm)
        df.addRow("하루 마감 시각", self.rollover)
        df.addRow("색 평균 반경", self.radius)

        # 현재 상태
        self.live = QLabel("감시가 꺼져 있습니다")
        self.live.setTextFormat(Qt.TextFormat.RichText)
        self.live.setWordWrap(True)
        lbox = QGroupBox("현재 상태")
        QVBoxLayout(lbox).addWidget(self.live)

        imp = QPushButton("기존 mess 폴더에서 가져오기…")
        imp.setToolTip("messenger_status.exe 가 있던 폴더의 config.json(좌표)과 logs(기록)를 가져옵니다")
        imp.clicked.connect(self._import)
        note = QLabel("상태는 결과 페이지의 '메신저 상태'에서 볼 수 있습니다. "
                      "기록은 data\\messenger\\logs 에 날짜별 txt 로 남습니다.")
        note.setStyleSheet("color: gray;")
        note.setWordWrap(True)

        left = QVBoxLayout()
        left.addWidget(self.enabled)
        left.addLayout(crow)
        left.addWidget(tbox)
        left.addWidget(abox)
        left.addWidget(obox, 1)
        right = QVBoxLayout()
        right.addWidget(lbox)
        right.addWidget(dbox)
        right.addWidget(imp)
        right.addWidget(note)
        right.addStretch()
        lay = QHBoxLayout(self)
        lay.addLayout(left, 3)
        lay.addLayout(right, 2)

        for w in (self.tx, self.ty, self.ax, self.ay, self.tolerance, self.interval, self.confirm,
                  self.rollover, self.radius):
            w.valueChanged.connect(self._apply)
        self.name.editingFinished.connect(self._apply)
        self.load()

    # 데이터 ↔ 화면
    @property
    def cfg(self):
        return self.editor.data.messenger

    def load(self):
        self._loading = True
        c = self.cfg
        self.enabled.setChecked(c.enabled)
        self.capture.setChecked(c.capture_window)
        self.name.setText(c.target.name)
        self.tx.setValue(c.target.x)
        self.ty.setValue(c.target.y)
        self.ax.setValue(c.anchor.x)
        self.ay.setValue(c.anchor.y)
        self.tolerance.setValue(c.anchor_tolerance)
        self.interval.setValue(c.poll_interval)
        self.confirm.setValue(c.confirm_count)
        self.rollover.setValue(c.rollover_hour)
        self.radius.setValue(c.sample_radius)
        self.anchor_rgb.setText(f"RGB {tuple(c.anchor.rgb)}" if c.anchor.x else "미설정 (검사 안 함)")
        self.others.setRowCount(0)
        for p in c.others:
            r = self.others.rowCount()
            self.others.insertRow(r)
            for col, v in enumerate((p.name, str(p.x), str(p.y))):
                self.others.setItem(r, col, QTableWidgetItem(v))
            pick = QPushButton("좌표 따기")
            pick.clicked.connect(lambda _=False, row=r: self._repick_other(row))
            self.others.setCellWidget(r, 3, pick)
        self._loading = False
        self.refresh_target_now()

    def _apply(self, *_):
        if self._loading:
            return
        c = self.cfg
        c.enabled = self.enabled.isChecked()
        c.target = Point(self.name.text().strip(), self.tx.value(), self.ty.value())
        if (self.ax.value(), self.ay.value()) != (c.anchor.x, c.anchor.y):
            c.anchor.x, c.anchor.y = self.ax.value(), self.ay.value()
            c.anchor.rgb = self._read_rgb(c.anchor.x, c.anchor.y) or c.anchor.rgb
            self.anchor_rgb.setText(f"RGB {tuple(c.anchor.rgb)}" if c.anchor.x else "미설정 (검사 안 함)")
        c.anchor_tolerance = self.tolerance.value()
        c.poll_interval = self.interval.value()
        c.confirm_count = self.confirm.value()
        c.rollover_hour = self.rollover.value()
        c.sample_radius = self.radius.value()
        self.refresh_target_now()
        self.editor.changed(refresh=False)

    def _source(self):
        """색을 읽을 곳. '가려져도 읽기'면 메신저 창을 찍고, 창이 옮겨졌으면 좌표를 같이 옮겨 보여 준다."""
        c = self.cfg
        if not c.capture_window:
            return self.editor.backend
        got = grab_messenger(c.window)
        if not isinstance(got, tuple):
            self._capture_error = str(got)
            return WindowFrame(None, (c.window.x, c.window.y))
        self._capture_error = ""
        image, (wx, wy) = got
        if rebase(c, wx, wy):  # 창을 옮겼다 → 화면 좌표도 같이 옮겨서 표시
            self.load()
            self.editor.changed(refresh=False)
        return WindowFrame(image, (c.window.x, c.window.y))

    def _read_rgb(self, x: int, y: int, source=None) -> tuple[int, int, int] | None:
        try:
            rgb = average_color(source or self._source(), x, y, self.cfg.sample_radius * 2 + 1)
        except ValueError:
            return None
        return tuple(int(round(v)) for v in rgb)

    def _toggle_capture(self, on: bool):
        if self._loading:
            return
        c = self.cfg
        if on:
            if not c.ready:
                QMessageBox.warning(self, "가려져도 읽기", "주 대상 좌표를 먼저 지정하세요.")
                self._set_capture(False)
                return
            ref = window_at(c.target.x, c.target.y)
            if ref is None:
                QMessageBox.warning(self, "가려져도 읽기",
                                    "주 대상 좌표에서 메신저 창을 찾지 못했습니다. 메신저 창이 보이게 한 뒤 다시 켜세요.")
                self._set_capture(False)
                return
            if ref.exe.lower().startswith("apphelper"):
                QMessageBox.warning(self, "가려져도 읽기",
                                    "주 대상 좌표가 Apphelper 창에 가려져 있습니다. 편집 화면을 옮겨 메신저가 보이게 한 뒤 다시 켜세요.")
                self._set_capture(False)
                return
            c.window = ref  # 지금 화면 좌표 = 지금 창 위치 기준
            got = grab_messenger(ref)
            if not isinstance(got, tuple):
                QMessageBox.warning(self, "가려져도 읽기", f"메신저 창을 캡처하지 못했습니다: {got}")
                self._set_capture(False)
                return
        c.capture_window = on
        self.refresh_target_now()
        self.editor.changed(refresh=False)

    def _set_capture(self, on: bool):
        self._loading = True
        self.capture.setChecked(on)
        self._loading = False

    def _test_capture(self):
        c = self.cfg
        ref = c.window if c.window.ready else (window_at(c.target.x, c.target.y) if c.ready else None)
        if ref is None:
            QMessageBox.warning(self, "메신저 창 캡처 시험",
                                "메신저 창을 알 수 없습니다. 주 대상 좌표를 메신저 아이콘에 지정한 뒤(메신저가 보이게) 다시 누르세요.")
            return
        got = grab_messenger(ref)
        if not isinstance(got, tuple):
            QMessageBox.warning(self, "메신저 창 캡처 시험", str(got))
            return
        image, (wx, wy) = got
        dark = float(image.mean()) < 8
        origin = (c.window.x, c.window.y) if c.window.ready else (wx, wy)
        frame = WindowFrame(image, origin)
        try:
            state = KOR.get(classify(average_color(frame, c.target.x, c.target.y, c.sample_radius * 2 + 1)),
                            "색이 애매함")
        except ValueError:
            state = "좌표가 창 밖"
        text = (f"{ref.exe} 창을 직접 찍은 그림입니다 (빨간 원 = 주 대상). 주 대상: {state}\n"
                + ("⚠ 그림이 검게 찍혔습니다. 이 PC 에서는 '가려져도 읽기'를 쓸 수 없습니다." if dark else
                   "메신저 창이 다른 창에 덮인 상태에서도 그림이 제대로 보이면 '가려져도 읽기'를 켜서 쓸 수 있습니다."))
        show_capture(self, text, image, (c.target.x - origin[0], c.target.y - origin[1]))

    def refresh_target_now(self):
        c = self.cfg
        if not c.ready:
            self.target_now.setText("좌표를 먼저 지정하세요")
            return
        source = self._source()  # 창이 옮겨졌으면 여기서 좌표가 먼저 맞춰진다
        rgb = self._read_rgb(c.target.x, c.target.y, source=source)
        if rgb is None:
            if c.capture_window:
                self.target_now.setText(f"⚠ {self._capture_error or '좌표가 메신저 창 밖입니다'}")
            else:
                self.target_now.setText("⚠ 화면 밖 좌표입니다")
            return
        state = classify(rgb)
        name = KOR.get(state, "색이 애매함 — 아이콘 정중앙으로 다시 지정하세요")
        self.target_now.setText(f"{name}  (RGB {rgb})")

    # 좌표 따기
    def _pick(self):
        pt = pick_coordinate(self.editor.backend, [self.editor])
        if pt and self.cfg.capture_window:
            self._source()  # 메신저 창이 옮겨졌으면 기존 좌표를 먼저 지금 위치로 맞춘다
        return pt

    def _pick_target(self):
        pt = self._pick()
        if pt:
            self.tx.setValue(pt[0])
            self.ty.setValue(pt[1])

    def _pick_anchor(self):
        pt = self._pick()
        if pt:
            rgb = self._read_rgb(*pt) or (self._read_rgb(*pt, source=self.editor.backend)
                                         if self.cfg.capture_window else None)
            self.cfg.anchor = Anchor(pt[0], pt[1], rgb or (255, 255, 255))  # 그 자리의 지금 색이 기준색
            self.load()
            self.editor.changed(refresh=False)

    def _add_other(self):
        name, ok = QInputDialog.getText(self, "사람 추가", "이름")
        name = name.strip()
        if not ok or not name:
            return
        pt = self._pick()
        if pt:
            self.cfg.others.append(Point(name, pt[0], pt[1]))
            self.load()
            self.editor.changed(refresh=False)

    def _repick_other(self, row: int):
        pt = self._pick()
        if pt and 0 <= row < len(self.cfg.others):
            p = self.cfg.others[row]
            self.cfg.others[row] = Point(p.name, pt[0], pt[1])
            self.load()
            self.editor.changed(refresh=False)

    def _delete_other(self):
        row = self.others.currentRow()
        if 0 <= row < len(self.cfg.others):
            del self.cfg.others[row]
            self.load()
            self.editor.changed(refresh=False)

    def _import(self):
        if self.log_dir is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "mess (messenger_status.exe) 폴더 선택")
        if not folder:
            return
        cfg, n = import_mess_folder(Path(folder), MessengerLog(self.log_dir))
        if cfg is None and n == 0:
            QMessageBox.warning(self, "가져오기", "이 폴더에서 config.json 이나 logs 를 찾지 못했습니다.")
            return
        lines = []
        if cfg is not None:
            cfg.enabled = True
            self.editor.data.messenger = cfg
            lines.append(f"좌표 설정을 가져왔습니다 (추가 인원 {len(cfg.others)}명).")
        lines.append(f"기록 파일 {n}개를 가져왔습니다.")
        self.load()
        self.editor.changed(refresh=False)
        QMessageBox.information(self, "가져오기", "\n".join(lines)
                                + "\n\n기존 messenger_status.exe 는 꺼 두세요 (같이 켜 두면 두 번 기록됩니다).")

    def show_live(self, snapshot: list[dict[str, str]]):
        if not self.cfg.enabled:
            self.live.setText("감시가 꺼져 있습니다")
            return
        if not snapshot:
            self.live.setText("확인 중…" if self.cfg.ready else "주 대상 좌표를 지정하세요")
            return
        rows = []
        for p in snapshot:
            color = STATE_COLORS.get(p["state"], "#6b7280")
            since = f" <span style='color:gray'>· {p['since'][11:16]}부터</span>" if p["since"] else ""
            rows.append(f"<b>{p['name']}</b> <span style='color:{color}'>● {p['state']}</span>{since}")
        self.live.setText("<br>".join(rows))


def show_capture(parent, text: str, image, mark: tuple[int, int] | None) -> None:
    """메신저 창 캡처 시험 결과 창."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import QDialog, QDialogButtonBox
    from .picker import rgb_to_qimage
    dlg = QDialog(parent)
    dlg.setWindowTitle("메신저 창 캡처 시험")
    lay = QVBoxLayout(dlg)
    label = QLabel(text)
    label.setWordWrap(True)
    lay.addWidget(label)
    img = rgb_to_qimage(image)
    if mark is not None:
        p = QPainter(img)
        p.setPen(QPen(QColor("#ef4444"), 3))
        p.drawEllipse(QPoint(*mark), 12, 12)
        p.end()
    pix = QPixmap.fromImage(img)
    if pix.width() > 700 or pix.height() > 700:
        pix = pix.scaled(700, 700, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    pic = QLabel()
    pic.setPixmap(pix)
    lay.addWidget(pic)
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    box.accepted.connect(dlg.accept)
    lay.addWidget(box)
    dlg.exec()


# ------------------------------------------------------------------ PC 전원

class PowerPanel(QWidget):
    """종료/다시시작 예약 (기존 closet). 예약 상태는 서비스의 PowerScheduler 가 들고 있다."""

    def __init__(self, editor: "EditorWindow", power: PowerScheduler,
                 on_power_now: Callable[[str], None],
                 autostart: tuple[Callable[[], bool], Callable[[bool], None]] | None = None):
        super().__init__()
        self.editor = editor
        self.power = power
        self.on_power_now = on_power_now
        self.autostart = autostart

        self.clock = QLabel()
        f = self.clock.font()
        f.setPointSize(22)
        f.setBold(True)
        self.clock.setFont(f)
        self.clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state = QLabel("예약 없음")
        self.state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sf = self.state.font()
        sf.setPointSize(12)
        self.state.setFont(sf)

        self.mode_shutdown = QRadioButton("종료")
        self.mode_restart = QRadioButton("다시시작")
        self.mode_shutdown.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.mode_shutdown)
        group.addButton(self.mode_restart)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("예약 종류"))
        mode_row.addWidget(self.mode_shutdown)
        mode_row.addWidget(self.mode_restart)
        mode_row.addStretch()

        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm")
        self.time.setTime(QTime.currentTime().addSecs(3600))
        sched = QPushButton("예약")
        sched.clicked.connect(self._schedule_at)
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("예약 시각"))
        time_row.addWidget(self.time)
        time_row.addWidget(sched)
        time_row.addStretch()

        quick = QHBoxLayout()
        for label, minutes in (("+30분", 30), ("+1시간", 60), ("+2시간", 120), ("+3시간", 180)):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, m=minutes: self.power.schedule(time.time() + m * 60, self.mode))
            quick.addWidget(b)
        quick.addStretch()

        cancel = QPushButton("예약 취소")
        cancel.clicked.connect(self.power.cancel)
        restart = QPushButton("지금 다시시작")
        restart.clicked.connect(lambda: self._now("restart"))
        shut = QPushButton("지금 종료")
        shut.setStyleSheet("color: #b91c1c;")
        shut.clicked.connect(lambda: self._now("shutdown"))
        actions = QHBoxLayout()
        for b in (cancel, restart, shut):
            actions.addWidget(b)
        actions.addStretch()

        self.force = QCheckBox("강제 종료/다시시작 (저장 안 된 프로그램도 닫음)")
        self.force.setChecked(editor.data.settings.power_force)
        self.force.toggled.connect(self._set_force)
        opts = QVBoxLayout()
        opts.addWidget(self.force)
        if autostart is not None:
            self.auto = QCheckBox("윈도우 시작 시 Apphelper 자동 실행")
            self.auto.setChecked(autostart[0]())
            self.auto.toggled.connect(self._set_autostart)
            opts.addWidget(self.auto)
        note = QLabel("예약 1분 전에 알림 창이 떠서 취소할 수 있습니다. 결과 페이지에서 원격으로 예약·종료하려면 "
                      "설정에서 '결과 페이지에서 PC 종료/다시시작 허용'을 켜세요. 트레이에서 Apphelper 를 "
                      "종료하면 예약도 취소됩니다.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        opts.addWidget(note)

        box = QGroupBox("종료 / 다시시작 예약")
        bl = QVBoxLayout(box)
        bl.addWidget(self.clock)
        bl.addWidget(self.state)
        bl.addLayout(mode_row)
        bl.addLayout(time_row)
        bl.addLayout(quick)
        bl.addLayout(actions)
        bl.addLayout(opts)
        lay = QVBoxLayout(self)
        lay.addWidget(box)
        lay.addStretch()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        self.refresh()

    @property
    def mode(self) -> str:
        return "restart" if self.mode_restart.isChecked() else "shutdown"

    def _schedule_at(self):
        t = self.time.time()
        now = datetime.now()
        target = now.replace(hour=t.hour(), minute=t.minute(), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        self.power.schedule(target.timestamp(), self.mode)

    def _now(self, mode: str):
        name = MODE_NAMES[mode]
        if QMessageBox.question(self, f"지금 {name}", f"컴퓨터를 지금 {name}할까요?") \
                == QMessageBox.StandardButton.Yes:
            self.on_power_now(mode)

    def _set_force(self, on: bool):
        self.editor.data.settings.power_force = on
        self.editor.changed(refresh=False)

    def _set_autostart(self, on: bool):
        try:
            self.autostart[1](on)
        except OSError as e:
            QMessageBox.warning(self, "자동 실행", f"설정하지 못했습니다: {e}")
            self.auto.blockSignals(True)
            self.auto.setChecked(self.autostart[0]())
            self.auto.blockSignals(False)

    def refresh(self):
        self.clock.setText(datetime.now().strftime("%H:%M:%S"))
        st = self.power.state()
        at = st["scheduled_at"]
        if at is None:
            self.state.setText("예약 없음")
            self.state.setStyleSheet("")
            return
        when = datetime.fromtimestamp(at)
        day = "" if when.date() == datetime.now().date() else "내일 "
        remaining = at - time.time()
        self.state.setText(f"{day}{when:%H:%M} {MODE_NAMES[st['mode']]} 예정 (남은 시간 {_fmt_remaining(remaining)})")
        self.state.setStyleSheet("color: #b91c1c;" if remaining <= 60 else "")


class PowerWarning(QWidget):
    """예약 1분 전 알림 (항상 위). 비밀번호 없이 바로 취소할 수 있다."""

    def __init__(self, power: PowerScheduler):
        super().__init__(None, Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Dialog)
        self.power = power
        self.setWindowTitle("Apphelper")
        self.label = QLabel()
        f = self.label.font()
        f.setPointSize(13)
        self.label.setFont(f)
        cancel = QPushButton("예약 취소")
        cancel.setDefault(True)
        cancel.clicked.connect(self._cancel)
        lay = QVBoxLayout(self)
        lay.addWidget(self.label)
        lay.addWidget(cancel)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        self.refresh()

    def refresh(self):
        st = self.power.state()
        remaining = self.power.remaining()
        if st["scheduled_at"] is None or remaining is None:
            self.close()
            return
        self.label.setText(f"{int(max(0, remaining))}초 후 컴퓨터를 {MODE_NAMES[st['mode']]}합니다.")

    def _cancel(self):
        self.power.cancel()
        self.close()


# ------------------------------------------------------------------ 자동 로그인

def window_info_at(x: int, y: int):
    """화면 (x, y) 의 창 정보 dict (exe, title, x, y …). Windows 가 아니면 None. 테스트에서 바꿔 끼운다."""
    import sys
    if sys.platform != "win32":
        return None
    from ..wincapture import window_info_at as f
    return f(x, y)


class LoginPanel(QWidget):
    """자동 로그인 항목 설정과 실행 버튼. 항목은 editor.data.logins 에 바로 반영·자동 저장된다."""

    def __init__(self, editor: "EditorWindow", on_login: Callable[[list], str]):
        from PySide6.QtWidgets import QDoubleSpinBox, QListWidget, QListWidgetItem, QPlainTextEdit  # noqa: F401
        super().__init__()
        self.editor = editor
        self.on_login = on_login
        self._loading = False
        self._busy = False

        run_all = self.run_all_btn = QPushButton("전체 자동 로그인")
        run_all.setStyleSheet("font-weight:600; padding: 6px 14px;")
        run_all.setToolTip("체크된 항목을 위에서부터 차례로 로그인합니다. 열려 있으면 그 창에, 없으면 열어서 로그인합니다.")
        run_all.clicked.connect(lambda: self._run([t for t in self.targets if t.enabled]))
        top = QHBoxLayout()
        top.addWidget(run_all)
        hint = QLabel("버튼을 누를 때만 실행됩니다. 실행 중에는 마우스·키보드를 건드리지 마세요.")
        hint.setStyleSheet("color: gray;")
        top.addWidget(hint, 1)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select)
        self.list.itemChanged.connect(self._item_changed)
        add_p = QPushButton("+ 프로그램")
        add_p.clicked.connect(lambda: self._add("program"))
        add_w = QPushButton("+ 웹 사이트")
        add_w.clicked.connect(lambda: self._add("web"))
        delete = QPushButton("삭제")
        delete.clicked.connect(self._delete)
        lrow = QHBoxLayout()
        for b in (add_p, add_w, delete):
            lrow.addWidget(b)
        left = QVBoxLayout()
        left.addWidget(QLabel("항목 (체크 = 전체 자동 로그인에 포함)"))
        left.addWidget(self.list, 1)
        left.addLayout(lrow)

        # 선택한 항목 편집
        self.name = QLineEdit()
        self.kind = QLabel()
        self.exe = QLineEdit()
        self.exe.setPlaceholderText("예: AtMessenger.exe (창 지정으로 채움)")
        self.title = QLineEdit()
        self.title.setPlaceholderText("창 제목에 들어가는 글자 (비우면 검사 안 함)")
        pick_win = QPushButton("창 지정 (좌표 따기)")
        pick_win.setToolTip("로그인할 창(로그인 화면)을 한 번 클릭하면 실행 파일 이름과 창 제목을 채웁니다.")
        pick_win.clicked.connect(self._pick_window)
        self.found = QLabel()
        self.found.setStyleSheet("color: gray;")
        self.found.setWordWrap(True)
        self.path = QLineEdit()
        browse = QPushButton("찾아보기…")
        browse.clicked.connect(self._browse)
        prow = QHBoxLayout()
        prow.addWidget(self.path, 1)
        prow.addWidget(browse)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://…")
        self.user = QLineEdit()
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.id_pos, self.pw_pos = QLabel(), QLabel()
        id_pick, pw_pick = QPushButton("좌표 따기"), QPushButton("좌표 따기")
        id_clear, pw_clear = QPushButton("지우기"), QPushButton("지우기")
        id_pick.clicked.connect(lambda: self._pick_field("id"))
        pw_pick.clicked.connect(lambda: self._pick_field("pw"))
        id_clear.clicked.connect(lambda: self._clear_field("id"))
        pw_clear.clicked.connect(lambda: self._clear_field("pw"))
        idrow, pwrow = QHBoxLayout(), QHBoxLayout()
        for row, w in ((idrow, (self.id_pos, id_pick, id_clear)), (pwrow, (self.pw_pos, pw_pick, pw_clear))):
            row.addWidget(w[0], 1)
            row.addWidget(w[1])
            row.addWidget(w[2])
        self.enter = QCheckBox("마지막에 Enter 누르기")
        self.wait = _spin(1, 600, " 초")
        self.ready = QDoubleSpinBox()
        self.ready.setRange(0, 60)
        self.ready.setSingleStep(0.5)
        self.ready.setSuffix(" 초")
        run_one = self.run_one_btn = QPushButton("이 항목 로그인")
        run_one.clicked.connect(lambda: self._run([self.current] if self.current else []))

        self.form_box = QGroupBox("항목 설정")
        f = self.form = QFormLayout(self.form_box)
        f.addRow("이름", self.name)
        f.addRow("종류", self.kind)
        f.addRow("", pick_win)
        f.addRow("실행 파일 이름", self.exe)
        f.addRow("창 제목 포함 글자", self.title)
        f.addRow("", self.found)
        self.path_label = QLabel("실행 경로")
        f.addRow(self.path_label, prow)
        self.url_label = QLabel("웹 주소")
        f.addRow(self.url_label, self.url)
        f.addRow("아이디", self.user)
        f.addRow("비밀번호", self.pw)
        f.addRow("아이디 칸", idrow)
        f.addRow("비밀번호 칸", pwrow)
        f.addRow("", self.enter)
        f.addRow("열고 창 기다리기 (최대)", self.wait)
        f.addRow("창이 뜬 뒤 준비 대기", self.ready)
        f.addRow("", run_one)

        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMaximumHeight(110)
        self.result.setPlaceholderText("실행 결과")

        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addWidget(self.form_box, 5)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(body, 1)
        lay.addWidget(self.result)

        for w in (self.name, self.exe, self.title, self.path, self.url, self.user):
            w.editingFinished.connect(self._apply)
        self.pw.editingFinished.connect(self._apply_password)
        self.enter.toggled.connect(self._apply)
        self.wait.valueChanged.connect(self._apply)
        self.ready.valueChanged.connect(self._apply)
        self.load()

    # 데이터
    @property
    def targets(self):
        return self.editor.data.logins

    @property
    def current(self):
        row = self.list.currentRow()
        return self.targets[row] if 0 <= row < len(self.targets) else None

    def load(self, row: int | None = None):
        from PySide6.QtWidgets import QListWidgetItem
        self._loading = True
        keep = self.list.currentRow() if row is None else row
        self.list.clear()
        for t in self.targets:
            it = QListWidgetItem(self._label(t))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if t.enabled else Qt.CheckState.Unchecked)
            self.list.addItem(it)
        self._loading = False
        self.list.setCurrentRow(min(max(keep, 0), len(self.targets) - 1) if self.targets else -1)
        self._select(self.list.currentRow())

    @staticmethod
    def _label(t) -> str:
        from ..core.autologin import KINDS
        return f"{t.name or '(이름 없음)'}  ·  {KINDS.get(t.kind, '')}"

    def _select(self, row: int):
        if self._loading:
            return
        t = self.current
        self.form_box.setEnabled(t is not None)
        if t is None:
            return
        from ..core.autologin import KINDS
        self._loading = True
        web = t.kind == "web"
        self.name.setText(t.name)
        self.kind.setText(KINDS.get(t.kind, t.kind))
        self.exe.setText(t.exe)
        self.title.setText(t.title)
        self.path.setText(t.path)
        self.path_label.setText("브라우저 경로 (비우면 기본 브라우저)" if web else "실행 경로 (창이 없을 때 열기)")
        self.url.setText(t.url)
        self.url.setVisible(web)
        self.url_label.setVisible(web)
        self.user.setText(t.user)
        self.pw.clear()
        self.pw.setPlaceholderText("저장됨 (바꾸려면 새로 입력)" if t.secret else "")
        self.id_pos.setText(f"창 기준 ({t.id_x}, {t.id_y})" if (t.id_x or t.id_y) else "지정 안 함 (커서가 있는 칸)")
        self.pw_pos.setText(f"창 기준 ({t.pw_x}, {t.pw_y})" if (t.pw_x or t.pw_y) else "지정 안 함 (아이디 뒤 Tab)")
        self.enter.setChecked(t.press_enter)
        self.wait.setValue(t.launch_wait)
        self.ready.setValue(t.ready_delay)
        self.found.setText("")
        self._loading = False

    def _changed(self):
        self.editor.changed(refresh=False)

    def _apply(self, *_):
        t = self.current
        if self._loading or t is None:
            return
        t.name = self.name.text().strip()
        t.exe = self.exe.text().strip()
        t.title = self.title.text().strip()
        t.path = self.path.text().strip().strip('"')
        t.url = self.url.text().strip()
        t.user = self.user.text()
        t.press_enter = self.enter.isChecked()
        t.launch_wait = self.wait.value()
        t.ready_delay = float(self.ready.value())
        item = self.list.currentItem()
        if item is not None:
            self._loading = True
            item.setText(self._label(t))
            self._loading = False
        self._changed()

    def _apply_password(self):
        t = self.current
        if self._loading or t is None or not self.pw.text():
            return
        t.password = self.pw.text()  # 보호해서 저장 (평문은 남기지 않음)
        self.pw.clear()
        self.pw.setPlaceholderText("저장됨 (바꾸려면 새로 입력)")
        self._changed()

    def _item_changed(self, item):
        if self._loading:
            return
        row = self.list.row(item)
        if 0 <= row < len(self.targets):
            self.targets[row].enabled = item.checkState() == Qt.CheckState.Checked
            self._changed()

    def _add(self, kind: str):
        from ..core.autologin import LoginTarget
        self.targets.append(LoginTarget(name="새 웹 사이트" if kind == "web" else "새 프로그램", kind=kind))
        self.load(len(self.targets) - 1)
        self._changed()

    def _delete(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.targets):
            if QMessageBox.question(self, "삭제", f"'{self.targets[row].name}' 항목을 지울까요?") \
                    != QMessageBox.StandardButton.Yes:
                return
            del self.targets[row]
            self.load(row)
            self._changed()

    def _browse(self):
        t = self.current
        if t is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "실행 파일 선택", "", "프로그램 (*.exe *.lnk);;모든 파일 (*)")
        if path:
            self.path.setText(path.replace("/", "\\"))
            if t.kind == "program" and not self.exe.text() and path.lower().endswith(".exe"):
                self.exe.setText(Path(path).name)
            self._apply()

    # 좌표 따기
    def _pick(self):
        return pick_coordinate(self.editor.backend, [self.editor])

    def _pick_window(self):
        t = self.current
        pt = self._pick()
        if t is None or not pt:
            return
        info = window_info_at(*pt)
        if info is None:
            QMessageBox.warning(self, "창 지정", "그 자리에서 창을 찾지 못했습니다 (Windows 에서만 됩니다).")
            return
        self.exe.setText(info["exe"])
        if t.kind == "web":
            self.title.setText(info["title"])  # 페이지 제목 중 늘 들어가는 부분만 남기도록 고쳐 쓰면 된다
        self.found.setText(f"찾은 창: {info['exe']} · 제목 \"{info['title']}\"")
        self._apply()

    def _pick_field(self, which: str):
        t = self.current
        pt = self._pick()
        if t is None or not pt:
            return
        info = window_info_at(*pt)
        if info is None:
            QMessageBox.warning(self, "좌표 따기", "그 자리에서 창을 찾지 못했습니다 (Windows 에서만 됩니다).")
            return
        if t.exe and info["exe"].lower() != t.exe.lower():
            if QMessageBox.question(self, "좌표 따기",
                                    f"지정한 프로그램({t.exe})이 아니라 {info['exe']} 창입니다. 이 창 기준으로 저장할까요?") \
                    != QMessageBox.StandardButton.Yes:
                return
        if not t.exe:
            t.exe = info["exe"]
        rx, ry = pt[0] - info["x"], pt[1] - info["y"]
        if which == "id":
            t.id_x, t.id_y = rx, ry
        else:
            t.pw_x, t.pw_y = rx, ry
        self._select(self.list.currentRow())
        self._changed()

    def _clear_field(self, which: str):
        t = self.current
        if t is None:
            return
        if which == "id":
            t.id_x = t.id_y = 0
        else:
            t.pw_x = t.pw_y = 0
        self._select(self.list.currentRow())
        self._changed()

    # 실행
    def _run(self, targets: list):
        if self._busy or not targets:
            if not targets:
                self.result.setPlainText("실행할 항목이 없습니다 (체크된 항목이 없음).")
            return
        self._apply()
        self.editor.flush()
        self._busy = True
        self.run_all_btn.setEnabled(False)
        self.run_one_btn.setEnabled(False)
        self.result.setPlainText("실행 중… " + ", ".join(t.name for t in targets))
        from .dialogs import run_in_background
        snapshot = [type(t).from_dict(t.to_dict()) for t in targets]  # 실행 중 편집해도 섞이지 않게
        run_in_background(lambda: self.on_login(snapshot), self._done)

    def _done(self, text: str):
        self._busy = False
        self.run_all_btn.setEnabled(True)
        self.run_one_btn.setEnabled(True)
        self.result.setPlainText(text)
        self.editor.raise_()
