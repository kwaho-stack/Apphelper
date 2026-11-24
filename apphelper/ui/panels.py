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
from ..core.messenger import KOR, Anchor, MessengerLog, Point, classify, import_mess_folder
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

class MessengerPanel(QWidget):
    """메신저 상태 감시 설정. 바꾸면 editor.data.messenger 에 바로 반영되고 자동 저장된다."""

    def __init__(self, editor: "EditorWindow", log_dir: Path | None):
        super().__init__()
        self.editor = editor
        self.log_dir = log_dir
        self._loading = False

        self.enabled = QCheckBox("메신저 상태 감시 사용")
        self.enabled.toggled.connect(self._apply)

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

    def _read_rgb(self, x: int, y: int) -> tuple[int, int, int] | None:
        try:
            rgb = average_color(self.editor.backend, x, y, self.cfg.sample_radius * 2 + 1)
        except ValueError:
            return None
        return tuple(int(round(v)) for v in rgb)

    def refresh_target_now(self):
        c = self.cfg
        if not c.ready:
            self.target_now.setText("좌표를 먼저 지정하세요")
            return
        rgb = self._read_rgb(c.target.x, c.target.y)
        if rgb is None:
            self.target_now.setText("⚠ 화면 밖 좌표입니다")
            return
        state = classify(rgb)
        name = KOR.get(state, "색이 애매함 — 아이콘 정중앙으로 다시 지정하세요")
        self.target_now.setText(f"{name}  (RGB {rgb})")

    # 좌표 따기
    def _pick(self):
        return pick_coordinate(self.editor.backend, [self.editor])

    def _pick_target(self):
        pt = self._pick()
        if pt:
            self.tx.setValue(pt[0])
            self.ty.setValue(pt[1])

    def _pick_anchor(self):
        pt = self._pick()
        if pt:
            rgb = self._read_rgb(*pt)
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
