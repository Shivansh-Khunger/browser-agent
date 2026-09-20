"""Public browser-session interface and backend-neutral models.

- `interface.py` — async `BrowserSession` contract used by new code.
- `session.py` — legacy synchronous implementation retained during cutover.
- `aria.py` — parses the `aria_snapshot` text into the nodes the model sees.
- `dom.py` — the few JS snippets that must run inside the page.
- `playwright_patch.py` — a driver bug workaround, applied on launch.

Nodriver transport remains internal. Callers depend on package exports, never
transport or backend identities.
"""

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
from .nodriver_session import NodriverSession

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
