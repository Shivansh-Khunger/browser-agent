"""Async browser-state orchestration seam."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..browser.models import ActionResult, Observation
from .models import (
    ActionCapture,
    ActionRequest,
    CapturePolicy,
    CheckpointRef,
    CleanShutdownProof,
    DiagnosticRef,
    EpisodeLease,
    EpisodeMetadata,
    EpisodeOutcome,
    StateDelta,
)


@runtime_checkable
class BrowserStateAdapter(Protocol):
    """Own episode state; checkpoint invalidates its lease before returning."""

    async def open_episode(
        self,
        seed_checkpoint: CheckpointRef | None,
        policy: CapturePolicy,
        metadata: EpisodeMetadata,
    ) -> EpisodeLease: ...

    async def begin_action(self, lease: EpisodeLease, request: ActionRequest) -> ActionCapture: ...

    async def finish_action(
        self,
        capture: ActionCapture,
        result: ActionResult,
        observation: Observation | None,
    ) -> StateDelta: ...

    async def confirm_shutdown(self, lease: EpisodeLease, proof: CleanShutdownProof) -> None: ...

    async def checkpoint(self, lease: EpisodeLease, reason: str) -> CheckpointRef: ...

    async def close_episode(
        self, lease: EpisodeLease, outcome: EpisodeOutcome
    ) -> CheckpointRef: ...

    async def abort_episode(self, lease: EpisodeLease, error: BaseException) -> DiagnosticRef: ...

    async def restore(self, checkpoint: CheckpointRef) -> EpisodeLease: ...

    async def branch(self, checkpoint: CheckpointRef, count: int) -> list[EpisodeLease]: ...
