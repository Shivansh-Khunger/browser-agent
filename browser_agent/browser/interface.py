"""Public async browser-session interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .models import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    BrowserMetadata,
    Observation,
    SessionLifecycle,
)

if TYPE_CHECKING:
    from ..state.models import CheckpointRef


@runtime_checkable
class BrowserSession(Protocol):
    @property
    def config(self) -> BrowserConfig: ...

    @property
    def lifecycle(self) -> SessionLifecycle: ...

    @property
    def active_target_id(self) -> str | None: ...

    @property
    def metadata(self) -> BrowserMetadata | None: ...

    @property
    def fatal_error(self) -> BaseException | None: ...

    @property
    def restore_authority(self) -> CheckpointRef | None: ...

    @property
    def compatibility_warnings(self) -> tuple[str, ...]: ...

    async def start(self) -> None: ...

    async def observe(self) -> Observation: ...

    async def execute(self, action: BrowserAction) -> ActionResult: ...

    async def checkpoint(self, reason: str) -> CheckpointRef: ...

    async def invalidate(self, error: BaseException) -> None: ...

    async def close(self) -> None: ...
