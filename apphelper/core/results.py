"""GitHub Pages 에 올릴 결과 JSON 관리.

데이터는 날짜, 시간, 분, 결과로 최소화한다.

{
  "updated": "2026-10-11 14:23",
  "days": {
    "2026-10-11": {
      "9":  {"r": "실패", "w": [10, 40]},
      "14": {"r": "작동", "m": 23, "k": ["변함!"], "w": [10, 40]},
      "15": {"r": "대기", "w": [0, 59]}
    }
  }
}
r: 상태, m: 실행된 분, k: 핵심 crash 결과, w: 설정된 분 범위 [시작, 끝]
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

    def mark_checked(self, day: date, hours: dict[int, tuple[int, int]] | list[int]) -> None:
        """체크된 시간대와 설정된 분 범위를 반영한다. 아직 기록이 없는 시간은 '대기'.

        hours 는 {시간: (시작분, 끝분)}. 오늘 체크가 해제된 시간 중 '대기' 상태인 항목은 지운다.
        '대기' 항목의 분 범위는 현재 설정으로 맞추고, 이미 실행된 항목은 실행 당시 범위를 그대로 둔다.
        """
        if not isinstance(hours, dict):
            hours = {h: None for h in hours}
        key = day.isoformat()
        with self._lock:
            entries = self._days.setdefault(key, {})
            for h in list(entries):
                if int(h) not in hours and entries[h].get("r") == WAITING:
                    del entries[h]
            for h, window in hours.items():
                entry = entries.setdefault(str(h), {"r": WAITING})
                if window is not None and (entry.get("r") == WAITING or "w" not in entry):
                    entry["w"] = [int(window[0]), int(window[1])]

    def record(self, when: datetime, ok: bool, key_results: list[str] | None = None,
               window: tuple[int, int] | None = None) -> None:
        entry: dict[str, Any] = {"r": DONE if ok else FAILED, "m": when.minute}
        if key_results:
            entry["k"] = list(key_results)
        if window is not None:
            entry["w"] = [int(window[0]), int(window[1])]
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
                        entries[h] = {"r": FAILED, **({"w": entry["w"]} if "w" in entry else {})}

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

    def to_json(self, updated: datetime | None = None) -> dict[str, Any]:
        """updated 를 주면 갱신 시각(기기 현지 시각, 'YYYY-MM-DD HH:MM')을 함께 넣는다."""
        with self._lock:
            days = {
                d: {h: copy.deepcopy(self._days[d][h]) for h in sorted(self._days[d], key=int)}
                for d in sorted(self._days)
            }
        out: dict[str, Any] = {"days": days}
        if updated is not None:
            out["updated"] = f"{updated:%Y-%m-%d %H:%M}"
        return out
