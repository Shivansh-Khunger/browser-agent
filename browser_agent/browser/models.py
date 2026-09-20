"""Backend-neutral models for one asynchronous browser session.

These types are intentionally free of nodriver and storage details. Concrete browser
and state adapters translate their own identities into opaque strings at this boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType


def _empty_object_mapping() -> Mapping[str, object]:
    return {}


def _empty_int_mapping() -> Mapping[str, int]:
    return {}


class SessionLifecycle(StrEnum):
    NEW = "new"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


class OutcomeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    launch: float = 30.0
    navigation: float = 30.0
    action: float = 10.0
    settle: float = 8.0
    shutdown: float = 10.0

    def __post_init__(self) -> None:
        for name in ("launch", "navigation", "action", "settle", "shutdown"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} timeout must be finite and greater than zero")


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    headless: bool = False
    executable_path: Path | None = None
    allowed_domains: tuple[str, ...] = ()
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    locale: str | None = None
    timezone: str | None = None
    geolocation: tuple[float, float] | None = None
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserMetadata:
    executable: Path
    browser_version: str
    nodriver_version: str
    platform: str
    config_digest: str


@dataclass(frozen=True, slots=True)
class Viewport:
    width: float
    height: float
    device_scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0
    scale: float = 1.0
    scroll_x: float = 0.0
    scroll_y: float = 0.0


@dataclass(frozen=True, slots=True)
class TargetHandle:
    observation_id: str
    control_id: str

    def __str__(self) -> str:
        return f"[{self.observation_id}:{self.control_id}]"


@dataclass(frozen=True, slots=True)
class ScreenshotMetadata:
    screenshot_id: str
    observation_id: str
    active_target_id: str
    viewport: Viewport


@dataclass(frozen=True, slots=True)
class ContextNode:
    kind: str
    text: str
    frame_breadcrumb: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SemanticControl:
    handle: TargetHandle
    role: str
    name: str
    description: str = ""
    value: str | None = None
    filled: bool | None = None
    states: frozenset[str] = frozenset()
    tag: str = ""
    input_type: str = ""
    frame_breadcrumb: tuple[str, ...] = ()
    visible: bool = True
    bounds: tuple[float, float, float, float] | None = None
    fallback_reason: str | None = None
    potentially_sensitive: bool = False

    def __post_init__(self) -> None:
        if self.potentially_sensitive and self.value is not None:
            raise ValueError("potentially sensitive controls cannot expose values")


@dataclass(frozen=True, slots=True)
class UnsupportedRegion:
    reason: str
    frame_breadcrumb: tuple[str, ...] = ()
    origin: str | None = None
    bounds: tuple[float, float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    active_target_id: str
    url: str
    title: str
    document_generation: int
    frame_generations: Mapping[str, int]
    viewport: Viewport
    controls: tuple[SemanticControl, ...] = ()
    context: tuple[ContextNode, ...] = ()
    unsupported_regions: tuple[UnsupportedRegion, ...] = ()
    screenshot: ScreenshotMetadata | None = None
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    omitted_counts: Mapping[str, int] = field(default_factory=_empty_int_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "frame_generations", MappingProxyType(dict(self.frame_generations))
        )
        object.__setattr__(self, "omitted_counts", MappingProxyType(dict(self.omitted_counts)))
        for control in self.controls:
            if control.handle.observation_id != self.observation_id:
                raise ValueError("control handle belongs to another observation")
        if self.screenshot and self.screenshot.observation_id != self.observation_id:
            raise ValueError("screenshot belongs to another observation")


@dataclass(frozen=True, slots=True)
class BrowserAction:
    name: str
    arguments: Mapping[str, object] = field(default_factory=_empty_object_mapping)
    target: TargetHandle | None = None
    read_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


@dataclass(frozen=True, slots=True)
class ActionResult:
    status: OutcomeStatus
    message: str
    error_code: str | None = None
    retryable: bool = False
    details: Mapping[str, object] = field(default_factory=_empty_object_mapping)
    observation: Observation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


class BrowserAdapterError(Exception):
    """Typed failure at browser adapter boundary."""

    code = "browser_adapter_error"
    retryable = False
    fatal = False


class SessionStateError(BrowserAdapterError):
    code = "session_state"


class BrowserLaunchError(BrowserAdapterError):
    code = "launch_failed"
    fatal = True


class BrowserDisconnectedError(BrowserAdapterError):
    code = "browser_disconnected"
    fatal = True


class BrowserExitedError(BrowserAdapterError):
    code = "browser_exited"
    fatal = True


class BrowserShutdownError(BrowserAdapterError):
    code = "shutdown_failed"
    fatal = True


class BrowserTimeoutError(BrowserAdapterError):
    code = "timeout"


class ClosedTargetError(BrowserAdapterError):
    code = "closed_target"


class StaleTargetError(BrowserAdapterError):
    code = "stale_target"
    retryable = True


class BrowserEvaluationError(BrowserAdapterError):
    code = "evaluation_failed"
