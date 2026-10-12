"""시간대 슬롯의 블록 순서를 실제로 실행한다."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .crash import ScreenBackend, crash, is_on_screen, key_crash
from .model import AppData, KeyCrash, Slot

log = logging.getLogger(__name__)


class SlotError(RuntimeError):
    pass


def resolve_target(kc: KeyCrash, hour: int, backend: ScreenBackend) -> tuple[int, int, str]:
    """핵심 crash 를 누를 좌표와 설명을 정한다. 좌표 모드는 시간별 변동, 이미지 모드는 화면에서 찾기."""
    if kc.uses_image:
        if not kc.image:
            raise SlotError(f"핵심 crash '{kc.name}' 에 이미지가 없습니다")
        from .imagefind import decode_template, locate  # numpy 는 이미지 모드에서만 필요
        m = locate(backend, decode_template(kc.image), kc.match / 100)
        return m.x, m.y, f"이미지 일치도 {m.score:.0%}"
    x, y = kc.coord_at(hour)
    if not is_on_screen(x, y, backend.screen_size()):
        raise SlotError(f"핵심 crash '{kc.name}' 좌표 ({x}, {y})가 화면 밖입니다")
    return x, y, f"{hour}시 좌표"


@dataclass
class SlotResult:
    ok: bool
    key_results: list[str] = field(default_factory=list)
    error: str = ""


def run_slot(
    data: AppData,
    slot: Slot,
    backend: ScreenBackend,
    sleep: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> SlotResult:
    """슬롯의 블록을 순서대로 실행한다. 한 단계라도 실패하면 즉시 멈추고 실패로 돌려준다."""
    key_results: list[str] = []
    try:
        for block in slot.blocks:
            if should_stop():
                raise SlotError("실행이 중단되었습니다")
            if block.kind == "basic":
                action = data.action(block.ref)
                if action is None:
                    raise SlotError(f"기본동작 {block.ref} 이(가) 없습니다")
                for step in action.steps:
                    if should_stop():
                        raise SlotError("실행이 중단되었습니다")
                    if step.kind == "crash":
                        crash(backend, step.x, step.y)
                    else:
                        sleep(max(step.ms, 0) / 1000)
            else:
                kc = data.key_crash(block.ref)
                if kc is None:
                    raise SlotError(f"핵심 crash {block.ref} 이(가) 없습니다")
                x, y, _ = resolve_target(kc, slot.hour, backend)
                outcome = key_crash(backend, x, y, kc.wait_ms, kc.region, kc.tolerance, sleep=sleep)
                log.debug("핵심 crash %s @(%d,%d) %s → %s : %s", kc.name, x, y,
                          outcome.before, outcome.after, outcome.label)
                key_results.append(outcome.label)
    except Exception as e:  # noqa: BLE001 - 어떤 오류든 슬롯 실패로 기록
        log.debug("슬롯 %d시 실패: %s", slot.hour, e)
        return SlotResult(False, key_results, str(e))
    return SlotResult(True, key_results)
