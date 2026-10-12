"""입력 보호: 마우스 휠로 값이 바뀌지 않게 하는 필터, 비밀번호 대화상자."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

MIN_PASSWORD = 4


class NoWheelFilter(QObject):
    """숫자 칸(스핀박스)과 선택 상자(콤보박스)는 휠로 값을 바꾸지 않는다.

    대신 바깥 스크롤 영역(타임라인, 표 등)으로 휠을 넘겨 화면은 계속 스크롤되게 한다.
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox)):
            parent = obj.parentWidget()
            while parent is not None and not isinstance(parent, QAbstractScrollArea):
                parent = parent.parentWidget()
            if parent is not None:
                QApplication.sendEvent(parent.viewport(), event)
            return True
        return False


def install_no_wheel(app: QApplication) -> NoWheelFilter:
    f = NoWheelFilter(app)
    app.installEventFilter(f)
    return f


class _PasswordDialog(QDialog):
    def __init__(self, title: str, message: str, confirm: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(320)
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        self.error = QLabel()
        self.error.setStyleSheet("color: #b91c1c;")
        self.confirm = confirm
        form = QFormLayout()
        form.addRow("비밀번호", self.pw)
        if confirm:
            form.addRow("한 번 더", self.pw2)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._ok)
        box.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(message))
        lay.addLayout(form)
        lay.addWidget(self.error)
        lay.addWidget(box)

    def _ok(self):
        if self.confirm:
            if len(self.pw.text()) < MIN_PASSWORD:
                self.error.setText(f"{MIN_PASSWORD}자 이상 입력하세요.")
                return
            if self.pw.text() != self.pw2.text():
                self.error.setText("두 비밀번호가 다릅니다.")
                return
        self.accept()


def ask_new_password(parent: QWidget | None = None, first: bool = True) -> str | None:
    msg = ("편집 화면을 열 때 쓸 비밀번호를 정하세요.\n"
           "잊어버리면 앱 폴더의 data\\lock.json 파일을 지우면 다시 정할 수 있습니다."
           if first else "새 비밀번호를 입력하세요.")
    dlg = _PasswordDialog("비밀번호 설정" if first else "비밀번호 변경", msg, True, parent)
    return dlg.pw.text() if dlg.exec() else None


def ask_password(parent: QWidget | None = None, retry: bool = False) -> str | None:
    msg = "비밀번호가 틀렸습니다. 다시 입력하세요." if retry else "편집 화면을 열려면 비밀번호를 입력하세요."
    dlg = _PasswordDialog("Apphelper 잠금", msg, False, parent)
    return dlg.pw.text() if dlg.exec() else None
