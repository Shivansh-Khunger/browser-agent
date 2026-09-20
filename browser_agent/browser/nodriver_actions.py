# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Native navigate and click dispatch against a nodriver Tab."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from nodriver import cdp
from nodriver.cdp.dom import BackendNodeId, Node
from nodriver.core.tab import Tab

from .geometry import quad_center
from .models import ActionResult, BrowserConfig, OutcomeStatus, StaleTargetError, TimeoutConfig

_SCHEME = re.compile(r"^https?://", re.IGNORECASE)


async def navigate(tab: Tab, config: BrowserConfig, url: str) -> ActionResult:
    normalized = _normalize_url(url)
    if normalized is None:
        return ActionResult(
            OutcomeStatus.FAILED,
            f"invalid navigation URL: {url!r}",
            error_code="invalid_url",
        )
    if not _domain_allowed(normalized, config.allowed_domains):
        return ActionResult(
            OutcomeStatus.FAILED,
            f"navigation to {normalized!r} is outside the configured domain allowlist",
            error_code="domain_blocked",
        )

    await tab.send(cdp.page.enable())
    loaded = asyncio.Event()

    def _on_load(_event: cdp.page.LoadEventFired) -> None:
        loaded.set()

    tab.add_handler(cdp.page.LoadEventFired, _on_load)
    try:
        _frame_id, _loader_id, error_text, _is_download = await tab.send(
            cdp.page.navigate(normalized)
        )
        if error_text:
            return ActionResult(
                OutcomeStatus.FAILED,
                f"navigation failed: {error_text}",
                error_code="navigation_failed",
            )
        settled = True
        try:
            async with asyncio.timeout(config.timeouts.navigation):
                await loaded.wait()
        except TimeoutError:
            settled = False
    finally:
        tab.remove_handler(cdp.page.LoadEventFired, _on_load)

    if not settled:
        return ActionResult(
            OutcomeStatus.UNCERTAIN,
            "navigation was dispatched but the page did not report load "
            "completion before the timeout",
            error_code="settle_timeout",
        )
    # LoadEventFired can precede OOPIF target attachment and nested-frame AX
    # availability. Give Chromium one bounded settling window before observation.
    await asyncio.sleep(config.timeouts.settle)
    return ActionResult(OutcomeStatus.SUCCEEDED, "navigate completed")


async def click(tab: Tab, backend_node_id: BackendNodeId, timeouts: TimeoutConfig) -> ActionResult:
    try:
        await tab.send(cdp.dom.scroll_into_view_if_needed(backend_node_id=backend_node_id))
        box = await tab.send(cdp.dom.get_box_model(backend_node_id=backend_node_id))
        subtree = await tab.send(
            cdp.dom.describe_node(backend_node_id=backend_node_id, depth=-1, pierce=False)
        )
    except Exception as error:
        raise StaleTargetError(f"control geometry is unavailable: {error}") from error

    x, y = quad_center(box.content)
    hit_backend_id, _frame_id, _node_id = await tab.send(
        cdp.dom.get_node_for_location(round(x), round(y), include_user_agent_shadow_dom=True)
    )
    if hit_backend_id not in _subtree_backend_ids(subtree):
        raise StaleTargetError(
            "control was obstructed or moved before the click could be dispatched"
        )

    for event_type in ("mousePressed", "mouseReleased"):
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                event_type,
                x=x,
                y=y,
                button=cdp.input_.MouseButton("left"),
                buttons=1,
                click_count=1,
            )
        )
    await asyncio.sleep(timeouts.settle)
    return ActionResult(OutcomeStatus.SUCCEEDED, "click completed")


def _subtree_backend_ids(node: Node) -> set[BackendNodeId]:
    """A native click may land on a descendant (e.g. a <span> inside a <button>);
    accept any hit inside the target's own subtree, not only the target itself."""
    ids: set[BackendNodeId] = set()

    def walk(current: Node) -> None:
        ids.add(current.backend_node_id)
        for child in current.children or ():
            walk(child)
        for shadow_root in current.shadow_roots or ():
            walk(shadow_root)

    walk(node)
    return ids


def _normalize_url(raw: str) -> str | None:
    if not raw or not raw.strip():
        return None
    if "\\" in raw:
        # Chrome's WHATWG URL parser folds backslashes into '/' in the authority
        # (e.g. "https://evil\\@allowed.test/" resolves to host "evil"), which
        # diverges from urlparse's RFC-3986 reading used for the allowlist check
        # below. Rejecting backslashes outright closes that host-confusion gap.
        return None
    candidate = raw if _SCHEME.match(raw) else f"https://{raw}"
    parsed = urlparse(candidate)
    if not parsed.hostname:
        return None
    return candidate


def _domain_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    if not allowed_domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)
