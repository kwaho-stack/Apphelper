"""데이터 모델과 앱 폴더 안 JSON 저장."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HOURS = 24


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- 단계 / 블록

@dataclass
class Step:
    """기본동작의 한 단계. kind 는 'crash' 또는 'wait'."""
    kind: str
    x: int = 0
    y: int = 0
    ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "crash":
            return {"kind": "crash", "x": self.x, "y": self.y}
        return {"kind": "wait", "ms": self.ms}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Step":
        kind = d.get("kind")
        if kind == "crash":
            return cls("crash", x=int(d["x"]), y=int(d["y"]))
        if kind == "wait":
            return cls("wait", ms=int(d["ms"]))
        raise ValueError(f"알 수 없는 단계 종류: {kind!r}")

    def describe(self) -> str:
        return f"crash ({self.x}, {self.y})" if self.kind == "crash" else f"대기 {self.ms}ms"


@dataclass
class BasicAction:
    """재사용 블록. crash 와 대기 단계로만 구성한다."""
    name: str
    steps: list[Step] = field(default_factory=list)
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BasicAction":
        return cls(name=d["name"], steps=[Step.from_dict(s) for s in d.get("steps", [])], id=d["id"])


@dataclass
class KeyCrash:
    """핵심 crash. 배치 시간대에 따라 좌표가 기준좌표 + (시간차 × 변동값) 으로 바뀐다."""
    name: str
    base_hour: int = 0
    base_x: int = 0
    base_y: int = 0
    dx: int = 0
    dy: int = 0
    wait_ms: int = 1500
    region: int = 5
    tolerance: int = 10
    id: str = field(default_factory=new_id)

    def coord_at(self, hour: int) -> tuple[int, int]:
        diff = hour - self.base_hour
        return self.base_x + diff * self.dx, self.base_y + diff * self.dy

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KeyCrash":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass
class BlockRef:
    """시간대 슬롯에 놓인 블록. kind 는 'basic' 또는 'key', ref 는 라이브러리 항목 id."""
    kind: str
    ref: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BlockRef":
        if d.get("kind") not in ("basic", "key"):
            raise ValueError(f"알 수 없는 블록 종류: {d.get('kind')!r}")
        return cls(d["kind"], d["ref"])


@dataclass
class Slot:
    hour: int
    enabled: bool = False
    minute_start: int = 0
    minute_end: int = 59
    blocks: list[BlockRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hour": self.hour,
            "enabled": self.enabled,
            "minute_start": self.minute_start,
            "minute_end": self.minute_end,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Slot":
        return cls(
            hour=int(d["hour"]),
            enabled=bool(d.get("enabled", False)),
            minute_start=int(d.get("minute_start", 0)),
            minute_end=int(d.get("minute_end", 59)),
            blocks=[BlockRef.from_dict(b) for b in d.get("blocks", [])],
        )


@dataclass
class Settings:
    github_owner: str = ""
    github_repo: str = ""
    github_token: str = ""
    debug_log: bool = False
    running: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    @property
    def github_ready(self) -> bool:
        return bool(self.github_owner and self.github_repo and self.github_token)


@dataclass
class AppData:
    actions: list[BasicAction] = field(default_factory=list)
    key_crashes: list[KeyCrash] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=lambda: [Slot(h) for h in range(HOURS)])
    settings: Settings = field(default_factory=Settings)

    # 조회 도우미
    def action(self, action_id: str) -> BasicAction | None:
        return next((a for a in self.actions if a.id == action_id), None)

    def key_crash(self, key_id: str) -> KeyCrash | None:
        return next((k for k in self.key_crashes if k.id == key_id), None)

    def remove_action(self, action_id: str) -> None:
        self.actions = [a for a in self.actions if a.id != action_id]
        self._drop_refs("basic", action_id)

    def remove_key_crash(self, key_id: str) -> None:
        self.key_crashes = [k for k in self.key_crashes if k.id != key_id]
        self._drop_refs("key", key_id)

    def _drop_refs(self, kind: str, ref: str) -> None:
        for slot in self.slots:
            slot.blocks = [b for b in slot.blocks if not (b.kind == kind and b.ref == ref)]

    def enabled_hours(self) -> list[int]:
        return [s.hour for s in self.slots if s.enabled]

    def clone(self) -> "AppData":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "actions": [a.to_dict() for a in self.actions],
            "key_crashes": [k.to_dict() for k in self.key_crashes],
            "slots": [s.to_dict() for s in self.slots],
            "settings": self.settings.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppData":
        slots_by_hour = {s.hour: s for s in (Slot.from_dict(x) for x in d.get("slots", []))}
        return cls(
            actions=[BasicAction.from_dict(a) for a in d.get("actions", [])],
            key_crashes=[KeyCrash.from_dict(k) for k in d.get("key_crashes", [])],
            slots=[slots_by_hour.get(h, Slot(h)) for h in range(HOURS)],
            settings=Settings.from_dict(d.get("settings", {})),
        )


# ---------------------------------------------------------------- 저장소

def app_dir() -> Path:
    """포터블 앱 폴더. exe 로 실행하면 exe 가 있는 폴더, 소스로 실행하면 프로젝트 루트."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


class Store:
    """앱 폴더 안 data/ 에 설정과 데이터를 모두 저장한다 (레지스트리 사용 안 함)."""

    FILE_NAME = "config.json"

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else app_dir() / "data"

    @property
    def path(self) -> Path:
        return self.data_dir / self.FILE_NAME

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> AppData:
        if not self.path.exists():
            return AppData()
        with self.path.open(encoding="utf-8") as f:
            return AppData.from_dict(json.load(f))

    def save(self, data: AppData) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def wipe(self) -> None:
        """전체 데이터 삭제: 로컬 설정, 저장된 동작, 토큰, 디버그 로그를 한 번에 지운다."""
        if self.data_dir.exists():
            shutil.rmtree(self.data_dir)
