"""메신저: 미작동 시간, 주 대상 오프라인 휴대폰(텔레그램) 알림."""
import os
from datetime import datetime

import pytest

from apphelper.core.messenger import MessengerConfig, Point
from apphelper.core.model import AppData
from apphelper.service import Service
from tests.fakes import FakeScreen

RED, BLUE, GRAY = (220, 40, 40), (40, 40, 220), (128, 128, 128)


def setup(tmp_path, now, notify=True):
    d = AppData()
    m = d.messenger
    m.enabled, m.confirm_count, m.sample_radius, m.notify_offline = True, 1, 1, notify
    m.target = Point("홍길동", 10, 10)
    d.settings.telegram_token, d.settings.telegram_chat_id = "T", "C"
    sent = []
    screen = FakeScreen(40, 40, (255, 255, 255))
    svc = Service(d, screen, "", now=lambda: now["t"], messenger_log_dir=tmp_path / "logs",
                  notifier=lambda tok, chat, text: sent.append((tok, chat, text)))
    svc.notify = lambda text, wait=False: Service.notify(svc, text, wait=True)  # 테스트에서는 바로 보낸다
    return svc, screen, sent


def show(svc, screen, color):
    screen.fill(8, 8, 5, 5, color)
    svc.messenger_step()


def test_offline_alert_only_from_online_or_away(tmp_path):
    now = {"t": datetime(2026, 11, 26, 9, 0)}
    svc, screen, sent = setup(tmp_path, now)
    show(svc, screen, GRAY)                       # 처음부터 오프라인 → 알림 없음
    show(svc, screen, RED)
    now["t"] = datetime(2026, 11, 26, 18, 5)
    show(svc, screen, GRAY)                       # 온라인 → 오프라인
    assert sent == [("T", "C", "🔴 홍길동 오프라인 (18:05)")]
    show(svc, screen, BLUE)
    screen.fill(0, 0, 40, 40, (255, 255, 255))
    svc.data.messenger.anchor.x, svc.data.messenger.anchor.y = 30, 30
    svc.data.messenger.anchor.rgb = (0, 0, 0)
    svc.messenger_step()                          # 확인불가 (기준점 다름) → 무시
    svc.data.messenger.anchor.x = 0
    show(svc, screen, GRAY)                       # 자리비움 → (확인불가) → 오프라인 = 알림
    assert len(sent) == 2 and "last_notify" not in svc.last_notify and "보냄" in svc.last_notify
    svc.data.messenger.notify_offline = False
    show(svc, screen, RED)
    show(svc, screen, GRAY)
    assert len(sent) == 2                         # 꺼 두면 안 보낸다


def test_quiet_hours_pause_and_page_flag(tmp_path):
    now = {"t": datetime(2026, 11, 26, 23, 0)}
    svc, screen, sent = setup(tmp_path, now)
    svc.data.messenger.quiet = [(22 * 60, 7 * 60)]
    show(svc, screen, RED)
    assert svc.messenger_snapshot() == [] or svc.messenger_snapshot()[0]["state"] != "온라인"
    web = svc.messenger_web_data()
    assert web["off"] and web["off_reason"] == "미작동 시간 (22:00~07:00)"
    now["t"] = datetime(2026, 11, 27, 7, 0)       # 끝 시각부터 다시 감시
    show(svc, screen, RED)
    assert svc.messenger_snapshot()[0]["state"] == "온라인" and "off" not in svc.messenger_web_data()
    c = MessengerConfig.from_dict({"quiet": [[1320, 420], [600, 600], ["x", 1]]})
    assert c.quiet == [(1320, 420)]
    assert c.quiet_range(datetime(2026, 1, 1, 3, 0)) and not c.quiet_range(datetime(2026, 1, 1, 12, 0))
    assert MessengerConfig.from_dict(c.to_dict()).quiet == [(1320, 420)]


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_panel_quiet_and_telegram(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    import time
    from apphelper.core import telegram
    from tests.test_ui_panels import editor
    ed = editor(tmp_path)
    mp = ed.messenger_panel
    mp._add_quiet()
    assert ed.data.messenger.quiet == [(1320, 420)] and len(mp._quiet_edits) == 1
    mp._quiet_edits[0][0].setTime(mp._quiet_edits[0][0].time().addSecs(-3600))
    assert ed.data.messenger.quiet == [(1260, 420)]
    mp._delete_quiet(0)
    assert ed.data.messenger.quiet == []
    mp.notify.setChecked(True)
    mp.tg_token.setText("TOK")
    mp.tg_token.editingFinished.emit()
    assert ed.data.messenger.notify_offline and ed.data.settings.telegram_token == "TOK"
    monkeypatch.setattr(telegram, "find_chat_id", lambda token: "12345")
    sent = []
    monkeypatch.setattr(telegram, "send", lambda tok, chat, text: sent.append((tok, chat)))
    mp._find_chat()
    end = time.monotonic() + 5
    while mp.tg_chat.text() != "12345" and time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)
    assert ed.data.settings.telegram_chat_id == "12345"
    mp._test_notify()
    while not sent and time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)
    assert sent == [("TOK", "12345")]
    ed.close()


def test_github_route_dispatches_sealed_message(tmp_path):
    from pathlib import Path
    from apphelper.core.telegram import NOTIFY_EVENT, unseal
    from tests.test_results_publisher import FakeGitHub
    gh = FakeGitHub()
    d = AppData()
    s = d.settings
    s.github_owner, s.github_repo, s.github_token = "o", "r", "tok"
    s.telegram_token, s.telegram_chat_id, s.telegram_route = "123:ABC", "987", "github"
    svc = Service(d, FakeScreen(), "", transport=gh)
    msg = svc.notify("🔴 홍길동 오프라인 (18:05)", wait=True)
    assert "GitHub 에 알림 요청 보냄" in msg
    body, = gh.dispatched
    assert body["event_type"] == NOTIFY_EVENT and "홍길동" not in str(body) and "987" not in str(body)
    assert unseal("123:ABC", body["client_payload"]["m"]) == {"chat": "987", "text": "🔴 홍길동 오프라인 (18:05)"}
    # GitHub Actions 쪽(notify.yml)도 같은 이벤트 이름·같은 키 만들기를 쓴다
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "notify.yml").read_text(encoding="utf-8")
    assert f"types: [{NOTIFY_EVENT}]" in wf and 'b"apphelper-notify-v1", 100_000' in wf
    assert "secrets.TELEGRAM_TOKEN" in wf and "client_payload.m" in wf
    svc.data.settings.github_token = ""
    assert "실패" in svc.notify("x", wait=True)          # GitHub 설정이 없으면 이유를 알려 준다
