import pytest

from apphelper.core.model import AppData, BasicAction, BlockRef, KeyCrash, Step, Store
from apphelper.core.runner import run_slot
from tests.fakes import FakeScreen


def test_key_crash_coord_shift():
    kc = KeyCrash("k", base_hour=1, base_x=100, base_y=100, dy=30)
    assert kc.coord_at(1) == (100, 100)
    assert kc.coord_at(2) == (100, 130)
    assert kc.coord_at(3) == (100, 160)
    assert kc.coord_at(0) == (100, 70)  # 기준보다 이른 시간 → 음수 방향


def make_data():
    d = AppData()
    a = BasicAction("열기", [Step("crash", x=1, y=1), Step("wait", ms=200), Step("crash", x=2, y=2)])
    k = KeyCrash("확인", base_hour=1, base_x=10, base_y=1, dy=2, region=3)
    d.actions.append(a)
    d.key_crashes.append(k)
    d.slots[3].enabled = True
    d.slots[3].minute_start, d.slots[3].minute_end = 10, 40
    d.slots[3].blocks = [BlockRef("basic", a.id), BlockRef("key", k.id)]
    d.settings.github_token = "secret"
    return d, a, k


def test_store_roundtrip_and_wipe(tmp_path):
    store = Store(tmp_path / "data")
    d, a, k = make_data()
    store.save(d)
    loaded = store.load()
    assert loaded.to_dict() == d.to_dict()
    assert len(loaded.slots) == 24
    store.wipe()
    assert not (tmp_path / "data").exists()
    assert store.load().to_dict() == AppData().to_dict()


def test_remove_action_drops_refs():
    d, a, k = make_data()
    d.remove_action(a.id)
    assert [b.kind for b in d.slots[3].blocks] == ["key"]
    d.remove_key_crash(k.id)
    assert d.slots[3].blocks == []


def test_run_slot_order_and_key_result():
    d, a, k = make_data()
    s = FakeScreen()
    s.on_click = lambda x, y: s.fill(x - 1, y - 1, 3, 3, (255, 255, 255)) if (x, y) == (10, 5) else None
    slept = []
    res = run_slot(d, d.slots[3], s, sleep=slept.append)
    assert res.ok, res.error
    assert s.clicks == [(1, 1), (2, 2), (10, 5)]  # 3시 → y = 1 + 2*2
    assert res.key_results == ["변함!"]
    assert slept == [0.2, 1.5]


def test_run_slot_offscreen_key_fails():
    d, a, k = make_data()
    k.dy = 100
    res = run_slot(d, d.slots[3], FakeScreen(), sleep=lambda _: None)
    assert not res.ok and "화면 밖" in res.error


def test_run_slot_missing_ref_fails():
    d, a, k = make_data()
    d.actions.clear()
    res = run_slot(d, d.slots[3], FakeScreen(), sleep=lambda _: None)
    assert not res.ok


def test_profiles_switch_copy_rename_delete(tmp_path):
    d, a, k = make_data()
    assert d.profile == "기본" and d.profile_names() == ["기본"]
    d.new_profile("야간", copy_current=True)
    assert d.profile == "야간" and len(d.actions) == 1 and d.slots[3].enabled
    d.slots[3].enabled = False
    d.new_profile("빈")
    assert d.actions == [] and d.enabled_hours() == []
    d.switch_profile("기본")
    assert d.slots[3].enabled and d.action(a.id) is not None
    d.switch_profile("야간")
    assert not d.slots[3].enabled

    store = Store(tmp_path / "data")
    store.save(d)
    loaded = store.load()
    assert loaded.profile == "야간" and loaded.profile_names() == ["기본", "빈", "야간"]
    assert loaded.settings.github_token == "secret"

    loaded.rename_profile("밤")
    loaded.delete_profile()
    assert loaded.profile_names() == ["기본", "빈"]
    loaded.delete_profile()
    with pytest.raises(ValueError):
        loaded.delete_profile()
    with pytest.raises(ValueError):
        loaded.new_profile(loaded.profile)


def test_v1_config_migrates_to_default_profile():
    d, a, k = make_data()
    v1 = {"version": 1, "actions": [a.to_dict()], "key_crashes": [k.to_dict()],
          "slots": [s.to_dict() for s in d.slots], "settings": d.settings.to_dict()}
    loaded = AppData.from_dict(v1)
    assert loaded.profile == "기본" and loaded.action(a.id) and loaded.slots[3].enabled
