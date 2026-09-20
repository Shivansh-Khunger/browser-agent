"""Public browser-state contracts and immutable reference types."""

from .adapter import BrowserStateAdapter
from .artifacts import ArtifactStore
from .models import (
    ActionCapture,
    ActionRequest,
    ArtifactKind,
    ArtifactRef,
    CapturePolicy,
    CheckpointRef,
    DiagnosticRef,
    EpisodeLease,
    EpisodeMetadata,
    EpisodeOutcome,
    SecurityClass,
    StateDelta,
)

__all__ = [
    "ActionCapture",
    "ActionRequest",
    "ArtifactKind",
    "ArtifactRef",
    "ArtifactStore",
    "BrowserStateAdapter",
    "CapturePolicy",
    "CheckpointRef",
    "DiagnosticRef",
    "EpisodeLease",
    "EpisodeMetadata",
    "EpisodeOutcome",
    "SecurityClass",
    "StateDelta",
]
