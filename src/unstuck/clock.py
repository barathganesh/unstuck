import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

_PROCESS_EPOCH = uuid4()


class Clock(Protocol):
    epoch: UUID

    def monotonic(self) -> float: ...
    def utcnow(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    epoch = _PROCESS_EPOCH

    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    def __init__(self) -> None:
        self.epoch = uuid4()
        self.seconds = 0.0
        self.origin = datetime.now(UTC)

    def monotonic(self) -> float:
        return self.seconds

    def utcnow(self) -> datetime:
        return self.origin + timedelta(seconds=self.seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self.seconds += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
