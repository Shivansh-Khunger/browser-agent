"""Public async browser-session interface and backend-neutral models.

Nodriver transport remains internal. Callers depend on package exports, never
transport or backend identities.
"""

from typing import TYPE_CHECKING, Any

from .interface import BrowserSession
from .models import (
    ActionResult,
    BrowserAction,
    BrowserAdapterError,
    BrowserConfig,
    BrowserDisconnectedError,
    BrowserEvaluationError,
    BrowserExitedError,
    BrowserLaunchError,
    BrowserMetadata,
    BrowserShutdownError,
    BrowserTimeoutError,
    ClosedTargetError,
    ContextNode,
    Observation,
    ObservationLimits,
    OutcomeStatus,
    ScreenshotMetadata,
    SemanticControl,
    SessionLifecycle,
    SessionStateError,
    StaleTargetError,
    TargetHandle,
    TimeoutConfig,
    UnsupportedRegion,
    Viewport,
)

if TYPE_CHECKING:
    from .nodriver_session import NodriverSession


def __getattr__(name: str) -> Any:
    if name == "NodriverSession":
        from .nodriver_session import NodriverSession

        return NodriverSession
    raise AttributeError(name)


__all__ = [
    "ActionResult",
    "BrowserAction",
    "BrowserAdapterError",
    "BrowserConfig",
    "BrowserDisconnectedError",
    "BrowserEvaluationError",
    "BrowserExitedError",
    "BrowserLaunchError",
    "BrowserMetadata",
    "BrowserShutdownError",
    "BrowserTimeoutError",
    "BrowserSession",
    "NodriverSession",
    "ClosedTargetError",
    "ContextNode",
    "Observation",
    "ObservationLimits",
    "OutcomeStatus",
    "ScreenshotMetadata",
    "SemanticControl",
    "SessionLifecycle",
    "SessionStateError",
    "StaleTargetError",
    "TargetHandle",
    "TimeoutConfig",
    "UnsupportedRegion",
    "Viewport",
]
