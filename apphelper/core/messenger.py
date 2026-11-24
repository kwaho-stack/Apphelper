"""메신저 접속 상태(온라인/자리비움/오프라인) 감지·기록 — 기존 mess(status_monitor.py)를 옮긴 것.

특정 사람의 상태 아이콘 색을 주기적으로 읽어서
  - 같은 상태가 confirm_count 번 연속 읽히면 상태가 바뀐 것으로 보고 일별 txt 로그에 기록한다
    (기록 시각은 그 상태가 처음 보인 시각, 날짜는 새벽 rollover_hour 시 기준)
  - 기준점(anchor) 색이 등록값과 다르면(로그인 화면/창 가림) '확인불가'
  - 결과 페이지용 데이터(현재 상태, 오늘+어제 변경 로그, 추가 인원 현황)를 만든다
로그 형식은 mess 와 같다: 'YYYY-MM-DD HH:MM:SS  온라인'
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .crash import ScreenBackend, average_color

KOR = {"ON": "온라인", "AWAY": "자리비움", "OFF": "오프라인", "NA": "확인불가"}
CODE = {v: k for k, v in KOR.items()}


# ---------------------------------------------------------------- 설정

@dataclass
class Point:
    name: str = ""
    x: int = 0
    y: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Point":
        d = d or {}
        return cls(str(d.get("name", "") or ""), int(d.get("x", 0) or 0), int(d.get("y", 0) or 0))


@dataclass
class Anchor:
    x: int = 0
    y: int = 0
    rgb: tuple[int, int, int] = (255, 255, 255)

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "rgb": list(self.rgb)}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Anchor":
        d = d or {}
        rgb = d.get("rgb") or [255, 255, 255]
        return cls(int(d.get("x", 0) or 0), int(d.get("y", 0) or 0), tuple(int(v) for v in rgb[:3]))


@dataclass
class MessengerConfig:
    enabled: bool = False
    poll_interval: int = 5        # 몇 초마다 확인할지
    sample_radius: int = 3        # 아이콘 중심에서 평균낼 반경(px)
    rollover_hour: int = 3        # 하루 마감 시각(새벽 3시)
    confirm_count: int = 3        # 같은 상태가 N회 연속 읽혀야 기록(깜빡임 방지)
    anchor_tolerance: int = 45    # 기준점 색이 이만큼 벗어나면 '확인불가'
    max_web_events: int = 300     # 페이지에 보여줄 최근 이벤트 개수
    target: Point = field(default_factory=Point)
    anchor: Anchor = field(default_factory=Anchor)
    others: list[Point] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.target.x or self.target.y)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "poll_interval": self.poll_interval,
            "sample_radius": self.sample_radius, "rollover_hour": self.rollover_hour,
            "confirm_count": self.confirm_count, "anchor_tolerance": self.anchor_tolerance,
            "max_web_events": self.max_web_events, "target": self.target.to_dict(),
            "anchor": self.anchor.to_dict(), "others": [p.to_dict() for p in self.others],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "MessengerConfig":
        """Apphelper 형식과 mess 의 config.json 형식(poll_interval_sec 등)을 모두 읽는다."""
        d = d or {}
        c = cls()
        c.enabled = bool(d.get("enabled", False))
        c.poll_interval = max(1, int(d.get("poll_interval", d.get("poll_interval_sec", 5))))
        c.sample_radius = max(0, int(d.get("sample_radius", 3)))
        c.rollover_hour = max(0, min(23, int(d.get("rollover_hour", 3))))
        c.confirm_count = max(1, int(d.get("confirm_count", 3)))
        c.anchor_tolerance = max(0, int(d.get("anchor_tolerance", 45)))
        c.max_web_events = max(1, int(d.get("max_web_events", 300)))
        c.target = Point.from_dict(d.get("target"))
        c.anchor = Anchor.from_dict(d.get("anchor"))
        c.others = [Point.from_dict(p) for p in d.get("others", []) if isinstance(p, dict)]
        return c


# ---------------------------------------------------------------- 색 → 상태

def classify(rgb) -> str:
    """평균 RGB -> 'ON' / 'AWAY' / 'OFF' / 'UNKNOWN' (mess 와 같은 규칙)."""
    r, g, b = (int(round(v)) for v in rgb)
    chroma = max(r, g, b) - min(r, g, b)
    if chroma < 30:                                   # 무채색 = 회색 = 오프라인
        return "OFF"
    if r >= g and r >= b and (r - max(g, b)) > 25:    # 빨강 = 온라인
        return "ON"
    if b >= r and b >= g and (b - min(r, g)) > 25:    # 파랑(시계) = 자리비움
        return "AWAY"
    return "UNKNOWN"


def color_dist(a, b) -> float:
    return max(abs(float(a[i]) - float(b[i])) for i in range(3))


def logical_date(dt: datetime, rollover_hour: int) -> date:
    """새벽 rollover_hour 시 이전이면 전날로 친다."""
    d = dt.date()
    if dt.hour < rollover_hour:
        d -= timedelta(days=1)
    return d


def safe_name(name: str) -> str:
    return "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "_"


# ---------------------------------------------------------------- 일별 로그 (mess 와 같은 형식)

class MessengerLog:
    """주 대상: logs/날짜.txt, 추가 인원: logs/others/이름/날짜.txt"""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)

    def _dir(self, person: str | None) -> Path:
        return self.log_dir / "others" / safe_name(person) if person else self.log_dir

    def path(self, d: date, person: str | None = None) -> Path:
        return self._dir(person) / f"{d.isoformat()}.txt"

    def append(self, d: date, at: datetime, code: str, person: str | None = None) -> None:
        p = self.path(d, person)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{at:%Y-%m-%d %H:%M:%S}  {KOR[code]}\n")

    def read_day(self, d: date, person: str | None = None) -> list[dict[str, str]]:
        p = self.path(d, person)
        out = []
        if p.exists():
            for raw in p.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                ts, _, kor = raw.partition("  ")
                out.append({"time": ts, "state": kor, "day": d.isoformat()})
        return out

    def last_state(self, d: date, person: str | None = None) -> tuple[str | None, str | None]:
        events = self.read_day(d, person)
        if events:
            last = events[-1]
            if last["state"] in CODE:
                return CODE[last["state"]], last["time"]
        return None, None

    def import_from(self, src_logs: Path) -> int:
        """mess 의 logs 폴더를 가져온다 (이미 있는 날짜 파일은 건드리지 않음). 가져온 파일 수."""
        n = 0
        for src in Path(src_logs).rglob("*.txt"):
            dst = self.log_dir / src.relative_to(src_logs)
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
        return n


# ---------------------------------------------------------------- 추적

@dataclass
class Tracker:
    name: str | None  # None = 주 대상
    x: int
    y: int
    state: str | None = None
    since: str | None = None
    pending: str | None = None
    n: int = 0
    at: datetime | None = None

    def step(self, state: str, now: datetime, confirm: int, log: MessengerLog, rollover: int) -> bool:
        """같은 상태가 confirm 회 연속 읽히면 기록하고 True. 기록 시각은 처음 감지된 시각."""
        if state == self.state:
            self.pending, self.n, self.at = None, 0, None
            return False
        if state == self.pending:
            self.n += 1
        else:
            self.pending, self.n, self.at = state, 1, now
        if self.n < confirm:
            return False
        at = self.at
        log.append(logical_date(at, rollover), at, state, self.name)
        self.state = state
        self.since = f"{at:%Y-%m-%d %H:%M:%S}"
        self.pending, self.n, self.at = None, 0, None
        return True


class MessengerMonitor:
    """한 번 읽기(step)와 페이지 데이터 만들기. 스레드/타이머는 바깥(Service)이 맡는다."""

    def __init__(self, config: MessengerConfig, log: MessengerLog, now: datetime):
        self.config = config
        self.log = log
        self.day = logical_date(now, config.rollover_hour)
        self.main = self._tracker(None, config.target)
        self.others = [self._tracker(p.name, p) for p in config.others]

    def _tracker(self, name: str | None, p: Point) -> Tracker:
        state, since = self.log.last_state(self.day, name)
        return Tracker(name, p.x, p.y, state, since)

    def same_layout(self, config: MessengerConfig) -> bool:
        a, b = self.config, config
        return (a.target == b.target and a.others == b.others and a.rollover_hour == b.rollover_hour)

    def _anchor_ok(self, backend: ScreenBackend) -> bool:
        a = self.config.anchor
        if not a.x:
            return True  # 기준점 미설정 시 검사 생략
        size = self.config.sample_radius * 2 + 1
        return color_dist(average_color(backend, a.x, a.y, size), a.rgb) <= self.config.anchor_tolerance

    def read_state(self, backend: ScreenBackend, x: int, y: int) -> str:
        size = self.config.sample_radius * 2 + 1
        return classify(average_color(backend, x, y, size))

    def step(self, backend: ScreenBackend, now: datetime) -> tuple[bool, bool]:
        """한 번 확인. (무엇이든 바뀜, 주 대상이 바뀜)."""
        day = logical_date(now, self.config.rollover_hour)
        if day != self.day:  # 날짜(새벽 마감)가 넘어가면 현재 상태를 새 날 첫 기록으로 남긴다
            for tr in [self.main] + self.others:
                tr.state, tr.pending, tr.n, tr.at = None, None, 0, None
            self.day = day
        try:
            ok = self._anchor_ok(backend)
        except ValueError:
            ok = False
        confirm = max(1, self.config.confirm_count)
        any_changed = main_changed = False
        for tr in [self.main] + self.others:
            if not ok:
                state = "NA"
            else:
                try:
                    state = self.read_state(backend, tr.x, tr.y)
                except ValueError:  # 좌표가 화면 밖
                    state = "NA"
            if state == "UNKNOWN":
                continue
            if tr.step(state, now, confirm, self.log, self.config.rollover_hour):
                any_changed = True
                main_changed = main_changed or tr is self.main
        return any_changed, main_changed

    def snapshot(self) -> list[dict[str, str]]:
        """편집 화면 표시용 현재 상태."""
        return [{"name": tr.name or (self.config.target.name or "주 대상"),
                 "state": KOR.get(tr.state, "확인 전"), "since": tr.since or ""}
                for tr in [self.main] + self.others]

    def web_data(self, now: datetime) -> dict[str, Any]:
        """결과 페이지용 데이터 (mess 의 data.json 과 같은 내용)."""
        today = logical_date(now, self.config.rollover_hour)
        yest = today - timedelta(days=1)
        events = self.log.read_day(yest) + self.log.read_day(today)
        others = [{
            "name": tr.name,
            "state": KOR.get(tr.state, "확인불가"),
            "since": tr.since or "",
            "events": self.log.read_day(today, tr.name)[-30:][::-1],
        } for tr in self.others]
        return {
            "name": self.config.target.name,
            "current": KOR.get(self.main.state, "확인 전"),
            "since": self.main.since or "",
            "today": today.isoformat(),
            "yesterday": yest.isoformat(),
            "events": events[-self.config.max_web_events:][::-1],  # 최신순
            "others": others,
        }


def import_mess_folder(folder: Path, log: MessengerLog) -> tuple[MessengerConfig | None, int]:
    """mess(messenger_status.exe) 폴더에서 config.json 의 좌표 설정과 logs 를 가져온다."""
    import json
    folder = Path(folder)
    cfg = None
    cfg_path = folder / "config.json"
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            cfg = MessengerConfig.from_dict(json.load(f))
    logs_dir = folder / "logs"
    n = log.import_from(logs_dir) if logs_dir.is_dir() else 0
    return cfg, n
