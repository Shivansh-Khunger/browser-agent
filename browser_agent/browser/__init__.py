"""Public browser models and async transport seam.

- `session.py` — `BrowserSession`, the one object the tools act through.
- `aria.py` — parses the `aria_snapshot` text into the nodes the model sees.
- `dom.py` — the few JS snippets that must run inside the page.
- `playwright_patch.py` — a driver bug workaround, applied on launch.

Deliberately no imports here: `from browser_agent.browser import aria` then costs
nothing, so the pure parser stays testable without a browser installed. Callers
name the module they want — `from .browser.session import BrowserSession`.
"""

from .models import (
    ActionResult,
    BrowserAction,
    BrowserAdapterError,
    BrowserConfig,
    BrowserEvaluationError,
    BrowserLaunchError,
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
from .transport import BrowserTransport

__all__ = [
    "ActionResult",
    "BrowserAction",
    "BrowserAdapterError",
    "BrowserConfig",
    "BrowserEvaluationError",
    "BrowserLaunchError",
    "BrowserTimeoutError",
    "BrowserTransport",
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
