"""순서 편집 화면: 왼쪽 라이브러리, 오른쪽 0~23시 타임라인 (드래그 앤 드롭)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from PySide6.QtCore import QMimeData, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QHBoxLayout, QInputDialog, QLabel, QListView,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea, QSpinBox, QSplitter, QToolButton,
    QVBoxLayout, QWidget,
)

from ..core.crash import ScreenBackend, is_on_screen
from ..core.model import AppData, BasicAction, BlockRef, KeyCrash, Settings, Slot
from .dialogs import BasicActionDialog, KeyCrashDialog, SettingsDialog

MIME = "application/x-apphelper-block"
BASIC_COLOR = QColor("#dbeafe")
KEY_COLOR = QColor("#ffedd5")
WARN_COLOR = QColor("#fecaca")
CHIP_TEXT = QColor("#1f2937")
REF_ROLE = Qt.ItemDataRole.UserRole


def block_mime(block: BlockRef) -> QMimeData:
    mime = QMimeData()
    mime.setData(MIME, json.dumps(block.to_dict()).encode("utf-8"))
    return mime


def block_from_mime(mime: QMimeData) -> BlockRef | None:
    if not mime.hasFormat(MIME):
        return None
    try:
        return BlockRef.from_dict(json.loads(bytes(mime.data(MIME)).decode("utf-8")))
    except (ValueError, KeyError):
        return None


# ------------------------------------------------------------------ 라이브러리

class LibraryList(QListWidget):
    """드래그 원본 목록. 항목을 끌면 BlockRef 를 담은 mime 을 만든다."""

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def startDrag(self, _actions):
        item = self.currentItem()
        if item is None:
            return
        drag = QDrag(self)
        drag.setMimeData(block_mime(BlockRef(self.kind, item.data(REF_ROLE))))
        drag.exec(Qt.DropAction.CopyAction)


class LibraryPanel(QWidget):
    def __init__(self, editor: "EditorWindow"):
        super().__init__()
        self.editor = editor
        self.basic = LibraryList("basic")
        self.key = LibraryList("key")
        self.basic.itemDoubleClicked.connect(lambda _: self.edit_basic())
        self.key.itemDoubleClicked.connect(lambda _: self.edit_key())

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("<b>기본동작</b>"))
        lay.addWidget(self.basic)
        lay.addLayout(self._buttons(self.new_basic, self.edit_basic, self.delete_basic))
        lay.addWidget(QLabel("<b>핵심 crash</b>"))
        lay.addWidget(self.key)
        lay.addLayout(self._buttons(self.new_key, self.edit_key, self.delete_key))
        lay.addWidget(QLabel("항목을 오른쪽 시간대로 끌어다 놓으세요."))

    @staticmethod
    def _buttons(new, edit, delete) -> QHBoxLayout:
        row = QHBoxLayout()
        for text, fn in (("새로 만들기", new), ("수정", edit), ("삭제", delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        return row

    def refresh(self) -> None:
        data = self.editor.data
        for widget, items, color in ((self.basic, data.actions, BASIC_COLOR),
                                     (self.key, data.key_crashes, KEY_COLOR)):
            current = widget.currentItem().data(REF_ROLE) if widget.currentItem() else None
            widget.clear()
            for obj in items:
                if isinstance(obj, BasicAction):
                    text = f"{obj.name}  ·  {len(obj.steps)}단계"
                    tip = "\n".join(s.describe() for s in obj.steps) or "(단계 없음)"
                else:
                    text = f"★ {obj.name}  ·  기준 {obj.base_hour}시 ({obj.base_x}, {obj.base_y})"
                    tip = (f"변동 dx={obj.dx}, dy={obj.dy} / 대기 {obj.wait_ms}ms / "
                           f"영역 {obj.region}×{obj.region} / 허용오차 ±{obj.tolerance}")
                it = QListWidgetItem(text)
                it.setData(REF_ROLE, obj.id)
                it.setToolTip(tip)
                it.setBackground(color)
                it.setForeground(CHIP_TEXT)
                widget.addItem(it)
                if obj.id == current:
                    widget.setCurrentItem(it)

    def _selected(self, widget: QListWidget) -> str | None:
        it = widget.currentItem()
        return it.data(REF_ROLE) if it else None

    def new_basic(self):
        dlg = BasicActionDialog(BasicAction(f"기본동작 {len(self.editor.data.actions) + 1}"),
                                self.editor.backend, self.editor)
        if dlg.exec():
            self.editor.data.actions.append(dlg.action)
            self.editor.changed()

    def edit_basic(self):
        action = self.editor.data.action(self._selected(self.basic) or "")
        if action is None:
            return
        dlg = BasicActionDialog(action, self.editor.backend, self.editor)
        if dlg.exec():
            action.name, action.steps = dlg.action.name, dlg.action.steps
            self.editor.changed()

    def delete_basic(self):
        action = self.editor.data.action(self._selected(self.basic) or "")
        if action and self._confirm(action.name):
            self.editor.data.remove_action(action.id)
            self.editor.changed()

    def new_key(self):
        dlg = KeyCrashDialog(KeyCrash(f"핵심 crash {len(self.editor.data.key_crashes) + 1}",
                                      base_hour=datetime.now().hour),
                             self.editor.backend, self.editor)
        if dlg.exec():
            self.editor.data.key_crashes.append(dlg.kc)
            self.editor.changed()

    def edit_key(self):
        kc = self.editor.data.key_crash(self._selected(self.key) or "")
        if kc is None:
            return
        dlg = KeyCrashDialog(kc, self.editor.backend, self.editor)
        if dlg.exec():
            kc.__dict__.update(dlg.kc.__dict__)
            self.editor.changed()

    def delete_key(self):
        kc = self.editor.data.key_crash(self._selected(self.key) or "")
        if kc and self._confirm(kc.name):
            self.editor.data.remove_key_crash(kc.id)
            self.editor.changed()

    def _confirm(self, name: str) -> bool:
        return QMessageBox.question(
            self, "삭제", f"'{name}' 을(를) 삭제할까요?\n이 블록이 놓인 모든 시간대에서도 빠집니다."
        ) == QMessageBox.StandardButton.Yes


# ------------------------------------------------------------------ 타임라인

class BlockList(QListWidget):
    """한 시간대의 블록 줄. 라이브러리/다른 시간대에서 놓으면 추가, 같은 줄 안에서는 순서 변경."""

    def __init__(self, row: "SlotRow"):
        super().__init__()
        self.slot_row = row
        self._drag_row: int | None = None
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSpacing(3)
        self.setFixedHeight(46)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        QShortcut(QKeySequence.StandardKey.Delete, self, self._delete_current,
                  context=Qt.ShortcutContext.WidgetShortcut)

    # 드래그 시작: 같은 줄이면 이동, 다른 줄로 놓으면 복사
    def startDrag(self, _actions):
        item = self.currentItem()
        if item is None:
            return
        self._drag_row = self.row(item)
        drag = QDrag(self)
        drag.setMimeData(block_mime(self.slot_row.slot.blocks[self._drag_row]))
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
        self._drag_row = None

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME):
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        self.dragEnterEvent(e)

    def _drop_index(self, pos: QPoint) -> int:
        idx = self.indexAt(pos)
        if not idx.isValid():
            return self.count()
        rect = self.visualRect(idx)
        return idx.row() + (1 if pos.x() > rect.center().x() else 0)

    def dropEvent(self, e):
        block = block_from_mime(e.mimeData())
        if block is None:
            e.ignore()
            return
        index = self._drop_index(e.position().toPoint())
        blocks = self.slot_row.slot.blocks
        if e.source() is self and self._drag_row is not None:
            moved = blocks.pop(self._drag_row)
            if index > self._drag_row:
                index -= 1
            blocks.insert(index, moved)
        else:
            blocks.insert(index, block)
        # 원본 목록이 항목을 지우지 않도록 항상 Copy 로 끝낸다.
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()
        self.slot_row.editor.changed()
        self.setCurrentRow(index)

    def _menu(self, pos: QPoint):
        item = self.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            i = self.row(item)
            menu.addAction("복제", lambda: self._duplicate(i))
            menu.addAction("복사", lambda: self.slot_row.editor.set_clipboard([self.slot_row.slot.blocks[i]]))
            menu.addAction("삭제", lambda: self._delete(i))
            menu.addSeparator()
        self.slot_row.add_slot_actions(menu)
        menu.exec(self.mapToGlobal(pos))

    def _duplicate(self, i: int):
        blocks = self.slot_row.slot.blocks
        blocks.insert(i + 1, BlockRef(blocks[i].kind, blocks[i].ref))
        self.slot_row.editor.changed()

    def _delete(self, i: int):
        del self.slot_row.slot.blocks[i]
        self.slot_row.editor.changed()

    def _delete_current(self):
        if self.currentRow() >= 0:
            self._delete(self.currentRow())

    def refresh(self):
        editor = self.slot_row.editor
        data, hour = editor.data, self.slot_row.slot.hour
        screen = editor.backend.screen_size()
        self.clear()
        for b in self.slot_row.slot.blocks:
            if b.kind == "basic":
                obj = data.action(b.ref)
                text = obj.name if obj else "(삭제됨)"
                tip = "\n".join(s.describe() for s in obj.steps) if obj else ""
                color = BASIC_COLOR
            else:
                obj = data.key_crash(b.ref)
                if obj:
                    x, y = obj.coord_at(hour)
                    ok = is_on_screen(x, y, screen)
                    text = f"★ {obj.name} ({x},{y})" + ("" if ok else " ⚠")
                    tip = f"{hour}시 좌표 ({x}, {y})" + ("" if ok else "\n⚠ 화면 밖 좌표입니다. 실행 시 실패로 기록됩니다.")
                    color = KEY_COLOR if ok else WARN_COLOR
                else:
                    text, tip, color = "(삭제됨)", "", WARN_COLOR
            it = QListWidgetItem(text)
            it.setToolTip(tip)
            it.setBackground(color)
            it.setForeground(CHIP_TEXT)
            it.setSizeHint(QSize(it.sizeHint().width(), 30))
            self.addItem(it)


class SlotRow(QWidget):
    def __init__(self, editor: "EditorWindow", slot: Slot):
        super().__init__()
        self.editor = editor
        self.slot = slot
        self.enabled = QCheckBox(f"{slot.hour:02d}시")
        self.enabled.setFixedWidth(64)
        self.start = QSpinBox()
        self.end = QSpinBox()
        for s in (self.start, self.end):
            s.setRange(0, 59)
            s.setAlignment(Qt.AlignmentFlag.AlignRight)
            # 숫자 두 자리와 화살표 버튼이 모두 보이도록 넉넉히 잡는다 (Windows 11 스타일 대비).
            s.setMinimumWidth(s.fontMetrics().horizontalAdvance("00") + 48)
        self.blocks = BlockList(self)
        more = QToolButton()
        more.setText("⋯")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.slot_menu = QMenu(more)
        self.slot_menu.aboutToShow.connect(self._build_menu)
        more.setMenu(self.slot_menu)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.addWidget(self.enabled)
        lay.addWidget(self.start)
        lay.addWidget(QLabel("분 ~"))
        lay.addWidget(self.end)
        lay.addWidget(QLabel("분"))
        lay.addWidget(self.blocks, 1)
        lay.addWidget(more)

        self.enabled.toggled.connect(self._on_fields)
        self.start.valueChanged.connect(self._on_fields)
        self.end.valueChanged.connect(self._on_fields)

    def _build_menu(self):
        self.slot_menu.clear()
        self.add_slot_actions(self.slot_menu)

    def add_slot_actions(self, menu: QMenu):
        ed = self.editor
        menu.addAction("이 시간대 복사", lambda: ed.set_clipboard(list(self.slot.blocks)))
        paste_over = QAction("붙여넣기 (덮어쓰기)", menu)
        paste_append = QAction("붙여넣기 (뒤에 추가)", menu)
        for a in (paste_over, paste_append):
            a.setEnabled(bool(ed.clipboard))
        paste_over.triggered.connect(lambda: self._paste(replace=True))
        paste_append.triggered.connect(lambda: self._paste(replace=False))
        menu.addAction(paste_over)
        menu.addAction(paste_append)
        menu.addAction("모두 지우기", self._clear)

    def _paste(self, replace: bool):
        copied = [BlockRef(b.kind, b.ref) for b in self.editor.clipboard]
        self.slot.blocks = copied if replace else self.slot.blocks + copied
        self.editor.changed()

    def _clear(self):
        self.slot.blocks = []
        self.editor.changed()

    def _on_fields(self):
        if self._loading:
            return
        self.slot.enabled = self.enabled.isChecked()
        self.slot.minute_start = self.start.value()
        self.slot.minute_end = self.end.value()
        if self.slot.minute_end < self.slot.minute_start:
            self.end.setStyleSheet("color: #b91c1c;")
            self.end.setToolTip("끝 분이 시작 분보다 작습니다. 두 값을 바꿔 사용합니다.")
        else:
            self.end.setStyleSheet("")
            self.end.setToolTip("")
        self.editor.changed(refresh=False)

    _loading = False

    def refresh(self):
        self._loading = True
        self.enabled.setChecked(self.slot.enabled)
        self.start.setValue(self.slot.minute_start)
        self.end.setValue(self.slot.minute_end)
        self._loading = False
        self.blocks.refresh()


# ------------------------------------------------------------------ 편집 창

class EditorWindow(QMainWindow):
    """편집 내용은 바뀔 때마다 (짧게 모아서) on_save 로 넘겨 자동 저장한다."""

    data_changed = Signal()

    def __init__(self, data: AppData, backend: ScreenBackend,
                 on_save: Callable[[AppData], None],
                 on_test_publish: Callable[[Settings], str],
                 on_wipe: Callable[[], AppData]):
        super().__init__()
        self.setWindowTitle("Apphelper — 편집")
        self.resize(1100, 760)
        self.data = data.clone()
        self.backend = backend
        self.clipboard: list[BlockRef] = []
        self._on_save = on_save
        self._on_test_publish = on_test_publish
        self._on_wipe = on_wipe

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(lambda: self._on_save(self.data.clone()))

        self.library = LibraryPanel(self)

        timeline = QWidget()
        tl = QVBoxLayout(timeline)
        tl.setSpacing(0)
        header = QLabel("사용 · 실행 분 범위 · 블록 순서 (끌어서 순서 변경, 우클릭: 복제/복사/붙여넣기/삭제)")
        header.setStyleSheet("color: gray; padding: 4px;")
        tl.addWidget(header)
        self.rows = [SlotRow(self, slot) for slot in self.data.slots]
        for r in self.rows:
            tl.addWidget(r)
        tl.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(timeline)

        splitter = QSplitter()
        splitter.addWidget(self.library)
        splitter.addWidget(scroll)
        splitter.setSizes([320, 780])

        settings_btn = QPushButton("설정")
        settings_btn.clicked.connect(self.open_settings)
        self.status = QLabel()
        self.last_run = QLabel()
        self.last_run.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.last_run)
        bottom.addWidget(settings_btn)

        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(160)
        self.profile_box.activated.connect(lambda _: self.switch_profile(self.profile_box.currentText()))
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>프로필</b>"))
        top.addWidget(self.profile_box)
        for text, fn in (("새 프로필", lambda: self._new_profile(False)),
                         ("복제", lambda: self._new_profile(True)),
                         ("이름 변경", self._rename_profile),
                         ("삭제", self._delete_profile)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            top.addWidget(b)
        top.addStretch()
        hint = QLabel("트레이 실행 시 현재 선택된 프로필의 스케줄이 실행됩니다")
        hint.setStyleSheet("color: gray;")
        top.addWidget(hint)

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.addLayout(top)
        cl.addWidget(splitter, 1)
        cl.addLayout(bottom)
        self.setCentralWidget(central)
        self.refresh()

    def set_last_run(self, text: str):
        self.last_run.setText(f"마지막 실행: {text}" if text else "")
        self.last_run.setStyleSheet("color: #b91c1c;" if "실패" in text else "")

    def set_clipboard(self, blocks: list[BlockRef]):
        self.clipboard = [BlockRef(b.kind, b.ref) for b in blocks]

    def refresh(self):
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItems(self.data.profile_names())
        self.profile_box.setCurrentText(self.data.profile)
        self.profile_box.blockSignals(False)
        self.setWindowTitle(f"Apphelper — 편집 [{self.data.profile}]")
        self.library.refresh()
        for r in self.rows:
            r.refresh()
        w, h = self.backend.screen_size()
        on = len(self.data.enabled_hours())
        self.status.setText(f"화면 {w}×{h} · 사용 중인 시간대 {on}개 · 변경 내용은 자동 저장됩니다")

    def changed(self, refresh: bool = True):
        if refresh:
            self.refresh()
        else:
            on = len(self.data.enabled_hours())
            w, h = self.backend.screen_size()
            self.status.setText(f"화면 {w}×{h} · 사용 중인 시간대 {on}개 · 변경 내용은 자동 저장됩니다")
        self._save_timer.start()
        self.data_changed.emit()

    def flush(self):
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._on_save(self.data.clone())

    def open_settings(self):
        dlg = SettingsDialog(self.data.settings, self._on_test_publish, self._wipe, self)
        if dlg.exec():
            self.data.settings = dlg.settings
            self.changed(refresh=False)

    def _wipe(self):
        self._save_timer.stop()
        self.load(self._on_wipe())

    def load(self, data: AppData):
        self.data = data.clone()
        self._rebind()

    def _rebind(self):
        self.clipboard = []  # 다른 프로필의 블록 id 는 의미가 없다
        for r, slot in zip(self.rows, self.data.slots):
            r.slot = slot
        self.refresh()

    # ------------------------------------------------------------ 프로필
    def switch_profile(self, name: str):
        if name == self.data.profile or name not in self.data.profile_names():
            return
        self.data.switch_profile(name)
        self._rebind()
        self.changed(refresh=False)

    def _ask_name(self, title: str, default: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "프로필 이름", text=default)
        name = name.strip()
        if not ok or not name:
            return None
        if name in self.data.profile_names() and name != default:
            QMessageBox.warning(self, "프로필", f"'{name}' 프로필이 이미 있습니다.")
            return None
        return name

    def _new_profile(self, copy_current: bool):
        name = self._ask_name("프로필 복제" if copy_current else "새 프로필")
        if name:
            self.data.new_profile(name, copy_current=copy_current)
            self._rebind()
            self.changed(refresh=False)

    def _rename_profile(self):
        name = self._ask_name("이름 변경", self.data.profile)
        if name and name != self.data.profile:
            self.data.rename_profile(name)
            self.changed()

    def _delete_profile(self):
        if not self.data.profiles:
            QMessageBox.information(self, "프로필", "마지막 프로필은 삭제할 수 없습니다.")
            return
        if QMessageBox.question(self, "프로필 삭제",
                                f"'{self.data.profile}' 프로필과 그 안의 동작·스케줄을 삭제할까요?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.data.delete_profile()
        self._rebind()
        self.changed(refresh=False)

    def closeEvent(self, e):
        self.flush()
        super().closeEvent(e)
