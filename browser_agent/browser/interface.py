"""Public async browser-session interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    Observation,
    SessionLifecycle,
)


@runtime_checkable
class BrowserSession(Protocol):
    @property
    def config(self) -> BrowserConfig: ...

    @property
    def lifecycle(self) -> SessionLifecycle: ...

    @property
    def active_target_id(self) -> str | None: ...

    async def start(self) -> None: ...

    async def observe(self) -> Observation: ...

    async def execute(self, action: BrowserAction) -> ActionResult: ...

    async def close(self) -> None: ...
