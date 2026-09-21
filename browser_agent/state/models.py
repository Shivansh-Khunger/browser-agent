"""Public models for browser episode state and captured evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from ..browser.models import (
    BrowserEvidence,
    CookieChange,
    NetworkEvent,
    OutcomeStatus,
    StorageEvent,
    TargetHandle,
)


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
    compatibility_warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CleanShutdownProof:
    episode_id: str
    process_id: int
    exit_code: int

    def __post_init__(self) -> None:
        if self.process_id <= 0:
            raise ValueError("process ID must be greater than zero")
        if self.exit_code != 0:
            raise ValueError("clean shutdown requires a zero exit code")


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
    incompatible_nodriver_transitions: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        if self.max_queue_items <= 0 or self.max_artifact_bytes <= 0:
            raise ValueError("capture bounds must be greater than zero")
        if self.capture_timeout <= 0:
            raise ValueError("capture timeout must be greater than zero")
        if any(
            len(transition) != 2 or not all(transition)
            for transition in self.incompatible_nodriver_transitions
        ):
            raise ValueError("nodriver incompatibility transitions require two versions")


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
    pre_observation_digest: str | None = None
    pre_target_id: str | None = None
    pre_url: str | None = None
    read_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "redacted_input", MappingProxyType(dict(self.redacted_input)))


@dataclass(frozen=True, slots=True)
class ActionCapture:
    capture_id: str
    episode_id: str
    request: ActionRequest
    started_at: str | None = None
    started_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class StateDelta:
    delta_id: str
    episode_id: str
    task_id: str
    action_id: str
    pre_observation_id: str | None
    post_observation_id: str | None
    outcome: OutcomeStatus
    pre_observation_digest: str | None = None
    post_observation_digest: str | None = None
    pre_target_id: str | None = None
    post_target_id: str | None = None
    pre_url: str | None = None
    post_url: str | None = None
    target_changed: bool = False
    url_changed: bool = False
    cookie_changes: tuple[CookieChange, ...] = ()
    storage_events: tuple[StorageEvent, ...] = ()
    network_events: tuple[NetworkEvent, ...] = ()
    network_span: tuple[int, int] | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    human_interventions: tuple[str, ...] = ()
    omissions: tuple[str, ...] = ()
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: float | None = None
    complete: bool = True
    record: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class ActionEvidence:
    """Ephemeral evidence supplied to adapter; adapter persists only redacted forms."""

    browser: BrowserEvidence = field(default_factory=BrowserEvidence)
    human_interventions: tuple[str, ...] = ()
