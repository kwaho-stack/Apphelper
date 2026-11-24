import json
from datetime import datetime, timedelta

from apphelper.core.messenger import (
    MessengerConfig, MessengerLog, MessengerMonitor, Point, classify, import_mess_folder, logical_date,
)
from tests.fakes import FakeScreen

RED, BLUE, GRAY, WHITE = (220, 40, 40), (40, 80, 220), (150, 150, 150), (255, 255, 255)


def test_classify_like_mess():
    assert classify(RED) == "ON"
    assert classify(BLUE) == "AWAY"
    assert classify(GRAY) == "OFF"
    assert classify((120, 160, 90)) == "UNKNOWN"


def test_logical_date_rollover():
    assert logical_date(datetime(2026, 11, 24, 2, 59), 3).isoformat() == "2026-11-23"
    assert logical_date(datetime(2026, 11, 24, 3, 0), 3).isoformat() == "2026-11-24"


def setup(tmp_path, confirm=3):
    screen = FakeScreen(100, 60, WHITE)
    cfg = MessengerConfig(enabled=True, confirm_count=confirm, sample_radius=1,
                          target=Point("", 10, 10), others=[Point("한", 30, 10)])
    cfg.anchor.x, cfg.anchor.y = 50, 50
    log = MessengerLog(tmp_path / "logs")
    return screen, cfg, log


def paint(screen, x, y, color):
    screen.fill(x - 2, y - 2, 5, 5, color)


def test_state_needs_confirmations_and_logs_first_seen_time(tmp_path):
    screen, cfg, log = setup(tmp_path)
    t0 = datetime(2026, 11, 24, 18, 0, 0)
    mon = MessengerMonitor(cfg, log, t0)
    paint(screen, 10, 10, RED)
    paint(screen, 30, 10, GRAY)
    results = [mon.step(screen, t0 + timedelta(seconds=5 * i)) for i in range(3)]
    assert results == [(False, False), (False, False), (True, True)]
    assert log.path(t0.date()).read_text(encoding="utf-8") == "2026-11-24 18:00:00  온라인\n"
    assert log.path(t0.date(), "한").read_text(encoding="utf-8") == "2026-11-24 18:00:00  오프라인\n"

    # 한 번 깜빡인 건 기록하지 않는다
    paint(screen, 10, 10, BLUE)
    mon.step(screen, t0 + timedelta(seconds=20))
    paint(screen, 10, 10, RED)
    assert mon.step(screen, t0 + timedelta(seconds=25)) == (False, False)
    assert mon.main.state == "ON"


def test_anchor_mismatch_is_unreadable(tmp_path):
    screen, cfg, log = setup(tmp_path, confirm=1)
    t0 = datetime(2026, 11, 24, 18, 0, 0)
    mon = MessengerMonitor(cfg, log, t0)
    paint(screen, 10, 10, RED)
    mon.step(screen, t0)
    paint(screen, 50, 50, (30, 30, 30))  # 로그인 화면 등으로 기준점 색이 바뀜
    assert mon.step(screen, t0 + timedelta(seconds=5)) == (True, True)
    assert mon.main.state == "NA"
    assert log.read_day(t0.date())[-1]["state"] == "확인불가"


def test_restores_state_and_web_data(tmp_path):
    screen, cfg, log = setup(tmp_path, confirm=1)
    t0 = datetime(2026, 11, 24, 18, 0, 0)
    mon = MessengerMonitor(cfg, log, t0)
    paint(screen, 10, 10, RED)
    paint(screen, 30, 10, BLUE)
    mon.step(screen, t0)
    again = MessengerMonitor(cfg, log, t0 + timedelta(minutes=1))  # 앱을 다시 켜도 이어서
    assert again.main.state == "ON" and again.others[0].state == "AWAY"
    data = again.web_data(t0 + timedelta(minutes=1))
    assert data["current"] == "온라인" and data["since"] == "2026-11-24 18:00:00"
    assert data["today"] == "2026-11-24" and data["yesterday"] == "2026-11-23"
    assert data["events"] == [{"time": "2026-11-24 18:00:00", "state": "온라인", "day": "2026-11-24"}]
    assert data["others"][0]["name"] == "한" and data["others"][0]["state"] == "자리비움"


def test_day_rollover_records_current_state_again(tmp_path):
    screen, cfg, log = setup(tmp_path, confirm=1)
    t0 = datetime(2026, 11, 24, 2, 59, 0)
    mon = MessengerMonitor(cfg, log, t0)
    paint(screen, 10, 10, RED)
    mon.step(screen, t0)
    mon.step(screen, datetime(2026, 11, 24, 3, 0, 5))
    assert log.read_day(datetime(2026, 11, 23).date())[0]["state"] == "온라인"
    assert log.read_day(datetime(2026, 11, 24).date())[0]["time"] == "2026-11-24 03:00:05"


def test_import_mess_folder(tmp_path):
    src = tmp_path / "mess"
    (src / "logs" / "others" / "한").mkdir(parents=True)
    (src / "logs" / "2026-11-20.txt").write_text("2026-11-20 09:00:00  온라인\n", encoding="utf-8")
    (src / "logs" / "others" / "한" / "2026-11-20.txt").write_text("x\n", encoding="utf-8")
    (src / "config.json").write_text(json.dumps({
        "poll_interval_sec": 7, "target": {"name": "", "x": 101, "y": 202},
        "anchor": {"x": 5, "y": 6, "rgb": [250, 251, 252]},
        "others": [{"name": "한", "x": 1, "y": 2}], "github": {"token": "secret"}}), encoding="utf-8")
    log = MessengerLog(tmp_path / "data" / "logs")
    (tmp_path / "data" / "logs").mkdir(parents=True)
    (tmp_path / "data" / "logs" / "2026-11-20.txt").write_text("keep\n", encoding="utf-8")
    cfg, n = import_mess_folder(src, log)
    assert cfg.poll_interval == 7 and (cfg.target.x, cfg.target.y) == (101, 202)
    assert cfg.anchor.rgb == (250, 251, 252) and cfg.others[0].name == "한"
    assert n == 1  # 이미 있는 날짜 파일은 덮어쓰지 않음
    assert (tmp_path / "data" / "logs" / "2026-11-20.txt").read_text(encoding="utf-8") == "keep\n"
    assert MessengerConfig.from_dict(cfg.to_dict()) == cfg
