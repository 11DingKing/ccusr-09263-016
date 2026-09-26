"""可替换端口：时钟与标识生成器。

应用服务只依赖这里的抽象，测试可注入固定时钟与序列化 ID，
从而稳定复现状态变化。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """时钟端口：返回带时区的当前时刻（UTC）。"""

    def now(self) -> datetime:
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试用手动时钟。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock requires an aware datetime")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("ManualClock requires an aware datetime")
        self._now = moment.astimezone(timezone.utc)

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


class IdGenerator(Protocol):
    """标识生成端口。"""

    def new_id(self, prefix: str) -> str:
        ...


class UuidIdGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SequentialIdGenerator:
    """测试用确定性 ID。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}

    def new_id(self, prefix: str) -> str:
        with self._lock:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            return f"{prefix}_{self._counters[prefix]:04d}"
