"""매 시간 지정한 분 범위 안의 랜덤 시점에 슬롯을 실행하는 스케줄러.

실행 시각은 미리 정해두지 않고, 해당 시간이 시작될 때(또는 앱이 그 시간 중간에
시작되면 그 시점에) 새로 뽑는다.
"""
from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .model import AppData, Slot

log = logging.getLogger(__name__)


def pick_run_offset(minute_start: int, minute_end: int, rng: random.Random,
                    not_before: int = 0) -> int | None:
    """시간 시작부터의 실행 오프셋(초)을 랜덤으로 고른다.

    범위는 [minute_start:00, minute_end:59] 이고, not_before 초 이전은 제외한다.
    고를 수 있는 시점이 없으면 None.
    """
    lo_m, hi_m = sorted((max(0, min(59, minute_start)), max(0, min(59, minute_end))))
    lo = max(lo_m * 60, not_before)
    hi = hi_m * 60 + 59
    if lo > hi:
        return None
    return rng.randint(lo, hi)


@dataclass
class Plan:
    hour_start: datetime
    run_at: datetime | None  # None 이면 이번 시간은 실행하지 않음
    done: bool = False


def plan_hour(slot: Slot, now: datetime, rng: random.Random) -> Plan:
    """now 가 속한 시간의 실행 계획을 만든다."""
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    if not slot.enabled:
        return Plan(hour_start, None)
    elapsed = int((now - hour_start).total_seconds())
    offset = pick_run_offset(slot.minute_start, slot.minute_end, rng, not_before=elapsed)
    if offset is None:
        return Plan(hour_start, None)
    return Plan(hour_start, hour_start + timedelta(seconds=offset))


class Scheduler:
    """백그라운드 스레드에서 도는 스케줄러.

    execute(slot, data, run_at) 콜백이 실제 실행과 결과 기록을 맡는다.
    on_hour(now) 콜백은 매 시간이 시작될 때(그리고 시작 직후) 호출된다.
    """

    def __init__(
        self,
        data: AppData,
        execute: Callable[[Slot, AppData, datetime], None],
        on_hour: Callable[[datetime], None] | None = None,
        now: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
    ):
        self._data = data.clone()
        self._execute = execute
        self._on_hour = on_hour
        self._now = now
        self._rng = rng or random.SystemRandom()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._paused = not data.settings.running
        self._plan: Plan | None = None
        self._last_hour: datetime | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 외부 제어
    @property
    def paused(self) -> bool:
        return self._paused

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._wake.set()

    def update_data(self, data: AppData) -> None:
        """편집 내용을 반영한다. 현재 시간 슬롯의 설정이 바뀌었으면 이번 시간 계획을 다시 뽑는다."""
        with self._lock:
            old = self._data
            self._data = data.clone()
            if self._plan and not self._plan.done:
                h = self._plan.hour_start.hour
                if old.slots[h].to_dict() != self._data.slots[h].to_dict():
                    self._plan = None
        self._wake.set()

    @property
    def current_plan(self) -> Plan | None:
        return self._plan

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="apphelper-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def sleep(self, seconds: float) -> None:
        """종료 요청이 오면 바로 깨어나는 대기 (실행 중인 슬롯의 대기 단계용)."""
        self._stop.wait(max(seconds, 0))

    # ------------------------------------------------------------ 루프
    def tick(self) -> float:
        """한 번 판단하고, 다음 판단까지 기다릴 초를 돌려준다. (테스트에서 직접 호출 가능)"""
        now = self._now()
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        with self._lock:
            data = self._data
            is_hour_change = self._last_hour != hour_start
            # 시간이 바뀌었거나 update_data 로 계획이 지워졌으면 새로 뽑는다.
            if self._plan is None or self._plan.hour_start != hour_start:
                self._plan = plan_hour(data.slots[now.hour], now, self._rng)
                self._last_hour = hour_start
                log.debug("%d시 계획: %s", now.hour, self._plan.run_at)
            plan = self._plan

        if is_hour_change and self._on_hour:
            try:
                self._on_hour(now)
            except Exception:  # noqa: BLE001
                log.exception("on_hour 실패")

        if plan.run_at and not plan.done and now >= plan.run_at:
            plan.done = True
            if self._paused:
                log.debug("일시정지 중이라 %d시 실행을 건너뜀", now.hour)
            else:
                slot = data.slots[plan.hour_start.hour]
                try:
                    self._execute(slot, data, now)
                except Exception:  # noqa: BLE001
                    log.exception("슬롯 실행 콜백 실패")
            now = self._now()

        next_hour = hour_start + timedelta(hours=1)
        target = next_hour
        if plan.run_at and not plan.done and plan.run_at < target:
            target = plan.run_at
        return max(0.05, min((target - now).total_seconds(), 30.0))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                wait = self.tick()
            except Exception:  # noqa: BLE001
                log.exception("스케줄러 오류")
                wait = 5
            self._wake.wait(wait)
            self._wake.clear()
