"""PC 종료/다시시작 예약 — 기존 closet(자동 종료 타이머)을 옮긴 것.

- 시:분 또는 +30분 같은 예약, 예약 취소, 지금 종료/다시시작
- 결과 페이지에서 보낸 원격 명령(apphelper-remote 브랜치 request.json 의 "power")
  {"id", "action": schedule|cancel|shutdown_now|restart_now, "at": ms, "mode": shutdown|restart,
   "issued_at": ms}
  2분이 지난 명령은 무시한다 (PC 가 꺼져 있을 때 누른 '지금 종료'가 나중에 켜자마자 실행되지 않게).
"""
from __future__ import annotations

import platform
import subprocess
import threading
import time
from typing import Any

MODES = ("shutdown", "restart")
MODE_NAMES = {"shutdown": "종료", "restart": "다시시작"}
COMMAND_MAX_AGE = 120  # 초
WARNING_SECONDS = 60   # 예약 시각 전에 알림 창을 띄우는 시간


def shutdown_command(force: bool = False, restart: bool = False) -> list[str]:
    system = platform.system()
    if system == "Windows":
        cmd = ["shutdown", "/r" if restart else "/s", "/t", "0"]
        if force:
            cmd.append("/f")
        return cmd
    if system == "Darwin":
        return ["osascript", "-e", 'tell app "System Events" to %s' % ("restart" if restart else "shut down")]
    return ["systemctl", "reboot" if restart else "poweroff"]


def shutdown_now(force: bool = False, restart: bool = False) -> None:
    kwargs: dict[str, Any] = {}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen(shutdown_command(force, restart), **kwargs)


def parse_command(cmd: Any, now: float | None = None) -> tuple[str, float | None, str | None] | None:
    """원격 명령을 검사해 (action, 예약시각 epoch초 또는 None, mode)를 돌려준다. 무효하면 None."""
    now = time.time() if now is None else now
    if not isinstance(cmd, dict):
        return None
    action = cmd.get("action")
    issued = cmd.get("issued_at")
    if action not in ("schedule", "cancel", "shutdown_now", "restart_now") \
            or not isinstance(issued, (int, float)) or isinstance(issued, bool):
        return None
    if now - issued / 1000 > COMMAND_MAX_AGE:
        return None
    if action == "schedule":
        at = cmd.get("at")
        mode = cmd.get("mode") or "shutdown"
        if not isinstance(at, (int, float)) or at / 1000 <= now or mode not in MODES:
            return None
        return action, at / 1000, mode
    return action, None, None


class PowerScheduler:
    """예약 상태 (스레드 안전). 실제 종료 실행과 알림 창은 GUI 가 tick() 결과를 보고 처리한다."""

    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self.scheduled_at: float | None = None
        self.mode = "shutdown"
        self.immediate = False   # 원격 '지금 종료/다시시작': 알림 없이 바로
        self.warned = False
        self.version = 0         # 바뀔 때마다 증가 (상태를 다시 올려야 하는지 판단용)

    def schedule(self, at: float, mode: str = "shutdown", immediate: bool = False) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        with self._lock:
            self.scheduled_at, self.mode, self.immediate = at, mode, immediate
            self.warned = False
            self.version += 1

    def cancel(self) -> None:
        with self._lock:
            self.scheduled_at = None
            self.immediate = self.warned = False
            self.version += 1

    def now_action(self, mode: str) -> None:
        self.schedule(self._clock(), mode, immediate=True)

    def apply_remote(self, parsed: tuple[str, float | None, str | None]) -> None:
        action, at, mode = parsed
        if action == "schedule":
            self.schedule(at, mode)
        elif action == "cancel":
            self.cancel()
        elif action == "shutdown_now":
            self.now_action("shutdown")
        elif action == "restart_now":
            self.now_action("restart")

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"scheduled_at": self.scheduled_at, "mode": self.mode}

    def remaining(self) -> float | None:
        with self._lock:
            return None if self.scheduled_at is None else self.scheduled_at - self._clock()

    def tick(self) -> str | None:
        """GUI 타이머가 1초마다 부른다. 'warn' (알림 창 띄우기) / 'fire' (지금 실행) / None."""
        with self._lock:
            if self.scheduled_at is None:
                return None
            remaining = self.scheduled_at - self._clock()
            if remaining <= 0:
                return "fire"
            if remaining <= WARNING_SECONDS and not self.warned and not self.immediate:
                self.warned = True
                return "warn"
            return None

    def take(self) -> str | None:
        """'fire' 처리: 예약을 비우고 실행할 mode 를 돌려준다."""
        with self._lock:
            if self.scheduled_at is None:
                return None
            mode = self.mode
            self.scheduled_at = None
            self.immediate = self.warned = False
            self.version += 1
            return mode
