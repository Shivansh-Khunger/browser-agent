"""Public browser-session interface and backend-neutral models.

- `interface.py` — async `BrowserSession` contract used by new code.
- `session.py` — legacy synchronous implementation retained during cutover.
- `aria.py` — parses the `aria_snapshot` text into the nodes the model sees.
- `dom.py` — the few JS snippets that must run inside the page.
- `playwright_patch.py` — a driver bug workaround, applied on launch.

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
