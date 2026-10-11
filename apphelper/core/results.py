"""GitHub Pages 에 올릴 결과 JSON 관리.

데이터는 날짜, 시간, 분, 결과로 최소화한다.

{
  "days": {
    "2026-10-11": {
      "9":  {"r": "대기"},
      "14": {"r": "작동", "m": 23, "k": ["변함!"]},
      "15": {"r": "실패", "m": 41}
    }
  }
}
"""
from __future__ import annotations

import copy
import threading
from datetime import date, datetime, timedelta
from typing import Any

WAITING = "대기"
DONE = "작동"
FAILED = "실패"

KEEP_DAYS = 7


class Results:
    """스레드 안전한 결과 저장소 (메모리 전용, 로컬 파일로 남기지 않는다)."""

    def __init__(self, raw: dict[str, Any] | None = None):
        self._lock = threading.Lock()
        days = (raw or {}).get("days", {})
        self._days: dict[str, dict[str, dict[str, Any]]] = (
            copy.deepcopy(days) if isinstance(days, dict) else {}
        )

    def merge_remote(self, raw: dict[str, Any] | None) -> None:
        """원격 JSON 을 합친다. 같은 날짜·시간은 로컬 값이 우선한다."""
        if not raw or not isinstance(raw.get("days"), dict):
            return
        with self._lock:
            for day, hours in raw["days"].items():
                if not isinstance(hours, dict):
                    continue
                local = self._days.setdefault(day, {})
                for hour, entry in hours.items():
                    local.setdefault(hour, copy.deepcopy(entry))

    def mark_checked(self, day: date, hours: list[int]) -> None:
        """체크된 시간대 목록을 반영한다. 아직 기록이 없는 시간은 '대기'.

        오늘 체크가 해제된 시간 중 '대기' 상태인 항목은 지운다.
        """
        key = day.isoformat()
        with self._lock:
            entries = self._days.setdefault(key, {})
            for h in list(entries):
                if int(h) not in hours and entries[h].get("r") == WAITING:
                    del entries[h]
            for h in hours:
                entries.setdefault(str(h), {"r": WAITING})

    def record(self, when: datetime, ok: bool, key_results: list[str] | None = None) -> None:
        entry: dict[str, Any] = {"r": DONE if ok else FAILED, "m": when.minute}
        if key_results:
            entry["k"] = list(key_results)
        with self._lock:
            self._days.setdefault(when.date().isoformat(), {})[str(when.hour)] = entry

    def sweep_missed(self, now: datetime) -> None:
        """이미 지나간 시간인데 아직 '대기'인 항목은 실행되지 못한 것이므로 '실패'로 바꾼다."""
        today = now.date().isoformat()
        with self._lock:
            for day, entries in self._days.items():
                for h, entry in entries.items():
                    if entry.get("r") != WAITING:
                        continue
                    if day < today or (day == today and int(h) < now.hour):
                        entries[h] = {"r": FAILED}

    def prune(self, today: date, keep_days: int = KEEP_DAYS) -> None:
        """최근 keep_days 일(오늘 포함)보다 오래된 결과를 지운다."""
        oldest = (today - timedelta(days=keep_days - 1)).isoformat()
        with self._lock:
            for day in [d for d in self._days if d < oldest]:
                del self._days[day]

    def get(self, day: str, hour: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self._days.get(day, {}).get(str(hour))
            return dict(entry) if entry else None

    def to_json(self) -> dict[str, Any]:
        with self._lock:
            days = {
                d: {h: copy.deepcopy(self._days[d][h]) for h in sorted(self._days[d], key=int)}
                for d in sorted(self._days)
            }
        return {"days": days}
