from apphelper.core.power import PowerScheduler, parse_command, shutdown_command

NOW = 1_800_000_000.0


def test_parse_command_like_closet():
    ms = NOW * 1000
    assert parse_command({"action": "cancel", "issued_at": ms}, NOW) == ("cancel", None, None)
    assert parse_command({"action": "restart_now", "issued_at": ms}, NOW) == ("restart_now", None, None)
    assert parse_command({"action": "schedule", "issued_at": ms, "at": ms + 60000, "mode": "restart"}, NOW) \
        == ("schedule", NOW + 60, "restart")
    assert parse_command({"action": "schedule", "issued_at": ms, "at": ms + 60000}, NOW)[2] == "shutdown"
    for bad in ({"action": "shutdown_now", "issued_at": ms - 121000},      # 2분 넘게 지난 명령
                {"action": "schedule", "issued_at": ms, "at": ms - 1000},  # 이미 지난 시각
                {"action": "schedule", "issued_at": ms, "at": ms + 1000, "mode": "x"},
                {"action": "format_c", "issued_at": ms}, {"action": "cancel"}, None, "x"):
        assert parse_command(bad, NOW) is None


def test_scheduler_warn_then_fire():
    clock = [NOW]
    p = PowerScheduler(clock=lambda: clock[0])
    p.schedule(NOW + 120, "restart")
    assert p.tick() is None
    clock[0] = NOW + 61
    assert p.tick() == "warn" and p.tick() is None
    clock[0] = NOW + 120
    assert p.tick() == "fire" and p.take() == "restart" and p.tick() is None


def test_immediate_skips_warning_and_cancel():
    p = PowerScheduler(clock=lambda: NOW)
    v = p.version
    p.apply_remote(("shutdown_now", None, None))
    assert p.tick() == "fire" and p.version > v
    p.cancel()
    assert p.tick() is None and p.state()["scheduled_at"] is None


def test_shutdown_command_flags():
    cmd = shutdown_command(force=True, restart=True)
    assert cmd[0] in ("shutdown", "osascript", "systemctl")
