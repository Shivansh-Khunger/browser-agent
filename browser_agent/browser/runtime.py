"""Internal owned-browser process seam for nodriver and lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    BrowserEvidence,
    BrowserMetadata,
    EvidenceWindow,
    Observation,
)


class RuntimeFailureKind(StrEnum):
    PROCESS_EXIT = "process_exit"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class RuntimeFailure:
    kind: RuntimeFailureKind
    exit_code: int | None = None


@runtime_checkable
class OwnedBrowser(Protocol):
    @property
    def process_id(self) -> int: ...

    @property
    def active_target_id(self) -> str | None: ...

    async def observe(self) -> Observation: ...

    async def execute(self, action: BrowserAction) -> ActionResult: ...

    async def begin_evidence(self) -> EvidenceWindow: ...

    async def finish_evidence(self, window: EvidenceWindow) -> BrowserEvidence: ...

    async def wait_for_failure(self) -> RuntimeFailure: ...

    async def request_close(self) -> None: ...

    async def close_connection(self) -> None: ...

    async def wait_for_exit(self) -> int: ...

    async def force_stop(self, timeout: float) -> None: ...


@runtime_checkable
class BrowserLauncher(Protocol):
    async def inspect(self, config: BrowserConfig) -> BrowserMetadata: ...

    async def launch(
        self, config: BrowserConfig, profile: Path, metadata: BrowserMetadata
    ) -> OwnedBrowser: ...
