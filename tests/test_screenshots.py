"""안변함 스크린샷, 상세 디버그, 시범 작동."""
import os
from datetime import datetime

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtGui")
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from apphelper.core.model import AppData, BasicAction, BlockRef, KeyCrash, Slot, Step  # noqa: E402
from apphelper.core.runner import run_slot  # noqa: E402
from apphelper.screenshots import ShotRecorder  # noqa: E402
from apphelper.service import Service  # noqa: E402


class Screen:
    """numpy 화면. change=True 면 클릭한 자리가 하얗게 바뀐다."""

    def __init__(self, change: bool):
        self.arr = np.zeros((80, 120, 3), np.uint8)
        self.change = change
        self.cursor = (0, 0)
        self.clicks = []

    def screen_size(self):
        return 120, 80

    def grab(self, left, top, w, h):
        return [[tuple(int(c) for c in px) for px in row] for row in self.arr[top:top + h, left:left + w]]

    def grab_array(self, left, top, w, h):
        return self.arr[top:top + h, left:left + w].copy()

    def get_cursor(self):
        return self.cursor

    def set_cursor(self, x, y):
        self.cursor = (x, y)

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.change:
            self.arr[y - 3:y + 4, x - 3:x + 4] = 255


def make_data(debug=False):
    d = AppData()
    a = BasicAction("열기", [Step("crash", x=10, y=10), Step("wait", ms=0), Step("crash", x=20, y=20)])
    k = KeyCrash("확인", base_hour=21, base_x=60, base_y=40, wait_ms=0)
    d.actions.append(a)
    d.key_crashes.append(k)
    d.slots[21].blocks = [BlockRef("basic", a.id), BlockRef("key", k.id)]
    d.slots[21].enabled = True
    d.slots[21].debug = debug
    return d


NOW = datetime(2026, 10, 26, 21, 10, 33)


@pytest.fixture(autouse=True, scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_screenshot_without_qt_app_does_not_crash(tmp_path):
    """Qt 앱이 없어도 (글자만 빼고) 저장된다 — 실제 앱에서는 항상 있음."""
    import subprocess
    import sys
    code = (
        "import os, sys; os.environ['QT_QPA_PLATFORM']='offscreen'; sys.path.insert(0, '.');"
        "from pathlib import Path; from tests.test_screenshots import Screen;"
        "from apphelper.screenshots import save_screenshot;"
        f"save_screenshot(Screen(False), Path(r'{tmp_path}') / 'a.png', (5, 5), 'x')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "a.png").exists()


def pngs(root):
    return sorted(p.name for p in root.rglob("*.png"))


def test_observer_events_in_order():
    d = make_data()
    events = []
    res = run_slot(d, d.slots[21], Screen(True), sleep=lambda _: None,
                   observer=lambda e, x, y, n: events.append((e, x, y, n)))
    assert res.ok
    assert events == [("crash", 10, 10, "열기"), ("crash", 20, 20, "열기"),
                      ("key_before", 60, 40, "확인"), ("key_after", 60, 40, "변함!")]


def test_unchanged_saves_one_screenshot(tmp_path):
    d = make_data()
    rec = ShotRecorder(Screen(False), tmp_path, 21, debug=False, now=lambda: NOW)
    res = run_slot(d, d.slots[21], rec.backend, sleep=lambda _: None, observer=rec)
    assert res.key_results == ["안변함!"]
    assert pngs(tmp_path) == ["21-10-33_21시_01_안변함.png"]
    assert rec.saved[0].parent.name == "2026-10-26"
    img = QImage(str(rec.saved[0]))
    assert (img.width(), img.height()) == (120, 80)
    assert img.pixelColor(60 + 18, 40).red() > 200  # 클릭 위치 표시(빨간 원)


def test_changed_saves_nothing_without_debug(tmp_path):
    d = make_data()
    rec = ShotRecorder(Screen(True), tmp_path, 21, debug=False, now=lambda: NOW)
    run_slot(d, d.slots[21], rec.backend, sleep=lambda _: None, observer=rec)
    assert pngs(tmp_path) == []


def test_debug_saves_every_action(tmp_path):
    d = make_data(debug=True)
    rec = ShotRecorder(Screen(True), tmp_path, 21, debug=True, now=lambda: NOW)
    run_slot(d, d.slots[21], rec.backend, sleep=lambda _: None, observer=rec)
    assert len(rec.saved) == 4
    assert [p.name.split("_", 3)[3] for p in rec.saved] == [
        "crash_열기.png", "crash_열기.png", "핵심crash_전_확인.png", "핵심crash_후_변함!.png"]


def test_service_execute_unchanged_screenshot_and_status(tmp_path):
    d = make_data()
    svc = Service(d, Screen(False), "", now=lambda: NOW, shots_dir=tmp_path / "shots")
    svc._execute(d.slots[21], d, NOW)
    assert len(pngs(tmp_path)) == 1
    assert "안변함!" in svc.last_status and "스크린샷 1장" in svc.last_status
    assert svc.results.get("2026-10-26", 21)["k"] == ["안변함!"]


def test_trial_run_does_not_touch_results(tmp_path):
    d = make_data(debug=True)
    screen = Screen(True)
    svc = Service(d, screen, "", now=lambda: NOW, shots_dir=tmp_path / "shots")
    msg = svc.trial_run(d, 21)
    assert "21시 시범 작동 완료" in msg and "변함!" in msg and "스크린샷 4장" in msg
    assert screen.clicks == [(10, 10), (20, 20), (60, 40)]
    assert svc.results.to_json() == {"days": {}}
    assert all("_시범_" in n for n in pngs(tmp_path))
    assert "블록이 없습니다" in svc.trial_run(d, 3)


def test_slot_debug_roundtrip():
    s = Slot(5, debug=True)
    assert Slot.from_dict(s.to_dict()).debug is True
    assert Slot.from_dict({"hour": 5}).debug is False
