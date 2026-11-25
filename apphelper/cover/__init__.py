"""화면 가리기: 앱 쪽 제어. 실제 가리기는 별도 프로세스(`--cover`, guard.py)가 한다.

입력 차단 후크는 응답이 늦으면 Windows 가 몰래 풀어 버리므로, 매크로·이미지 찾기로 바쁜
앱 프로세스와 분리해 가볍고 한가한 프로세스에서 돌린다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .guard import EXIT_EMERGENCY, EXIT_STOPPED, EXIT_UNLOCKED, EXIT_UNSUPPORTED  # noqa: F401

CREATE_NO_WINDOW = 0x08000000


def cover_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--cover"]
    return [sys.executable, str(Path(__file__).resolve().parents[2] / "run_apphelper.py"), "--cover"]


class CoverController:
    def __init__(self, command: list[str] | None = None):
        self.command = command or cover_command()
        self.proc: subprocess.Popen | None = None

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, verifier: dict[str, str]) -> None:
        if self.active:
            return
        kwargs = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, encoding="utf-8", **kwargs)
        self.proc.stdin.write(json.dumps(verifier) + "\n")
        self.proc.stdin.flush()

    def poll(self) -> int | None:
        """끝났으면 종료 코드(한 번만), 아직이거나 시작 안 했으면 None."""
        if self.proc is None:
            return None
        code = self.proc.poll()
        if code is not None:
            self.proc = None
        return code

    def stop(self, timeout: float = 5) -> None:
        """해제 없이 끝낸다 (테스트·앱 종료용)."""
        if not self.active:
            return
        try:
            self.proc.stdin.write("STOP\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            self.proc.kill()
