"""Public models for browser episode state and captured evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from ..browser.models import OutcomeStatus, TargetHandle


def _empty_object_mapping() -> Mapping[str, object]:
    return {}


class SecurityClass(StrEnum):
    REDACTED = "redacted"
    RESTRICTED = "restricted"


class ArtifactKind(StrEnum):
    ACTION = "action"
    OBSERVATION = "observation"
    SCREENSHOT = "screenshot"
    DOM = "dom"
    NETWORK = "network"
    CHECKPOINT = "checkpoint"
    DIAGNOSTIC = "diagnostic"


class EpisodeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


class StateAdapterError(Exception):
    """Typed failure at browser-state boundary."""

    code = "state_adapter_error"
    retryable = False
    fatal = False


class LeaseClosedError(StateAdapterError):
    code = "lease_closed"


class ArtifactNotFoundError(StateAdapterError):
    code = "artifact_not_found"


class ArtifactIntegrityError(StateAdapterError):
    code = "artifact_integrity_failed"
    fatal = True


class RestrictedStorageError(StateAdapterError):
    code = "restricted_storage_unavailable"
    fatal = True


class CheckpointError(StateAdapterError):
    code = "checkpoint_failed"
    fatal = True


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    content_id: str
    kind: ArtifactKind
    media_type: str
    schema_version: int
    encoding: str
    compression: str | None
    byte_length: int
    security_class: SecurityClass
    redaction_policy_version: str
    restricted_locator: str | None = None


@dataclass(frozen=True, slots=True)
class CheckpointRef:
    checkpoint_id: str
    episode_id: str
    parent_id: str | None
    reason: str
    clean_shutdown: bool
    schema_version: int = 1
    manifest: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticRef:
    diagnostic_id: str
    episode_id: str
    artifact: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class EpisodeLease:
    """Opaque authority for one unique writable episode profile."""

    lease_id: str
    episode_id: str
    parent_checkpoint_id: str | None = None


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    version: str
    redaction_policy_version: str
    optional_artifacts: frozenset[ArtifactKind] = frozenset()
    max_queue_items: int = 128
    max_artifact_bytes: int = 10_000_000
    capture_timeout: float = 5.0
    restricted_storage: bool = False
    retention_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_queue_items <= 0 or self.max_artifact_bytes <= 0:
            raise ValueError("capture bounds must be greater than zero")
        if self.capture_timeout <= 0:
            raise ValueError("capture timeout must be greater than zero")


@dataclass(frozen=True, slots=True)
class EpisodeMetadata:
    code_revision: str
    platform: str
    task_id: str | None = None
    browser_executable: str | None = None
    browser_version: str | None = None
    nodriver_version: str | None = None
    config_digest: str | None = None
    locale_override: str | None = None
    timezone_override: str | None = None


@dataclass(frozen=True, slots=True)
class ActionRequest:
    task_id: str
    action_id: str
    name: str
    redacted_input: Mapping[str, object] = field(default_factory=_empty_object_mapping)
    target: TargetHandle | None = None
    pre_observation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "redacted_input", MappingProxyType(dict(self.redacted_input)))


@dataclass(frozen=True, slots=True)
class ActionCapture:
    capture_id: str
    episode_id: str
    request: ActionRequest


@dataclass(frozen=True, slots=True)
class StateDelta:
    delta_id: str
    episode_id: str
    task_id: str
    action_id: str
    pre_observation_id: str | None
    post_observation_id: str | None
    outcome: OutcomeStatus
    artifacts: tuple[ArtifactRef, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    human_interventions: tuple[str, ...] = ()
