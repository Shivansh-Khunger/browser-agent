"""Internal async transport seam implemented by nodriver and deterministic fakes."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ActionResult, BrowserAction, BrowserConfig, Observation


@runtime_checkable
class BrowserTransport(Protocol):
    @property
    def active_target_id(self) -> str | None: ...

    async def start(self, config: BrowserConfig) -> None: ...

    async def observe(self) -> Observation: ...

    async def execute(self, action: BrowserAction) -> ActionResult: ...

    async def close(self) -> None: ...
