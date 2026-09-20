"""Public browser-state contracts and immutable reference types."""

from .adapter import BrowserStateAdapter
from .artifacts import ArtifactStore, LocalArtifactStore
from .local import LocalBrowserStateAdapter
from .models import (
    ActionCapture,
    ActionRequest,
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactRef,
    CapturePolicy,
    CheckpointError,
    CheckpointRef,
    DiagnosticRef,
    EpisodeLease,
    EpisodeMetadata,
    EpisodeOutcome,
    LeaseClosedError,
    RestrictedStorageError,
    SecurityClass,
    StateAdapterError,
    StateDelta,
)

__all__ = [
    "ActionCapture",
    "ActionRequest",
    "ArtifactKind",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "ArtifactRef",
    "ArtifactStore",
    "BrowserStateAdapter",
    "CapturePolicy",
    "CheckpointRef",
    "CheckpointError",
    "DiagnosticRef",
    "EpisodeLease",
    "EpisodeMetadata",
    "EpisodeOutcome",
    "LeaseClosedError",
    "LocalArtifactStore",
    "LocalBrowserStateAdapter",
    "RestrictedStorageError",
    "SecurityClass",
    "StateDelta",
    "StateAdapterError",
]
