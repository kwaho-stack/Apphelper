import random
from datetime import datetime, timedelta

from apphelper.core.model import AppData
from apphelper.core.scheduler import Scheduler, pick_run_offset, plan_hour


def test_pick_run_offset_within_range():
    rng = random.Random(1)
    for _ in range(2000):
        off = pick_run_offset(10, 40, rng)
        assert 600 <= off <= 40 * 60 + 59


def test_pick_run_offset_covers_range():
    rng = random.Random(2)
    minutes = {pick_run_offset(10, 12, rng) // 60 for _ in range(500)}
    assert minutes == {10, 11, 12}


def test_pick_run_offset_not_before():
    rng = random.Random(3)
    for _ in range(500):
        assert pick_run_offset(10, 40, rng, not_before=30 * 60) >= 30 * 60
    assert pick_run_offset(10, 40, rng, not_before=41 * 60) is None


def test_plan_hour_disabled():
    d = AppData()
    assert plan_hour(d.slots[5], datetime(2026, 1, 1, 5, 0), random.Random()).run_at is None


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_sched(clock, data=None, seed=0):
    data = data or AppData()
    runs, hours = [], []
    sched = Scheduler(data, lambda slot, d, when: runs.append((slot.hour, when)),
                      on_hour=hours.append, now=clock, rng=random.Random(seed))
    return sched, runs, hours


def test_scheduler_runs_once_per_hour_and_redraws_each_hour():
    d = AppData()
    for h in (1, 2):
        d.slots[h].enabled = True
        d.slots[h].minute_start, d.slots[h].minute_end = 10, 40
    clock = Clock(datetime(2026, 1, 1, 1, 0, 0))
    sched, runs, hours = make_sched(clock, d)

    plans = []
    end = datetime(2026, 1, 1, 3, 0, 0)
    while clock.t < end:
        sched.tick()
        if sched.current_plan and sched.current_plan not in plans:
            plans.append(sched.current_plan)
        clock.t += timedelta(seconds=1)

    assert [h for h, _ in runs] == [1, 2]
    for h, when in runs:
        assert when.hour == h and 10 <= when.minute <= 40
    # 시간이 시작될 때 매번 새로 뽑았는지
    assert [p.hour_start.hour for p in plans] == [1, 2]
    assert len(hours) == 2


def test_scheduler_paused_skips():
    d = AppData()
    d.slots[1].enabled = True
    d.settings.running = False
    clock = Clock(datetime(2026, 1, 1, 1, 0, 0))
    sched, runs, _ = make_sched(clock, d)
    while clock.t.hour == 1:
        sched.tick()
        clock.t += timedelta(seconds=5)
    assert runs == []


def test_scheduler_started_mid_hour_picks_future_time():
    d = AppData()
    d.slots[1].enabled = True
    d.slots[1].minute_start, d.slots[1].minute_end = 10, 40
    clock = Clock(datetime(2026, 1, 1, 1, 30, 0))
    sched, _, _ = make_sched(clock, d)
    sched.tick()
    assert sched.current_plan.run_at >= clock.t


def test_scheduler_wait_is_bounded():
    clock = Clock(datetime(2026, 1, 1, 1, 0, 0))
    sched, _, _ = make_sched(clock)
    assert 0 < sched.tick() <= 30
