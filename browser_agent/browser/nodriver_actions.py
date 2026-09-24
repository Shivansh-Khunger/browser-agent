# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Native navigate and click dispatch against a nodriver Tab."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
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

    await _dispatch_click(tab, x, y, timeouts)
    return ActionResult(OutcomeStatus.SUCCEEDED, "click completed")


async def click_at(tab: Tab, x: float, y: float, timeouts: TimeoutConfig) -> ActionResult:
    """Dispatch a validated coordinate click in top-level viewport space."""
    await _dispatch_click(tab, x, y, timeouts)
    return ActionResult(OutcomeStatus.SUCCEEDED, "coordinate click completed")


async def prepare_text_entry(tab: Tab, backend_node_id: BackendNodeId) -> ActionResult:
    """Focus and select a safe text-entry control without changing its value.

    CDP's ``Input.insertText`` produces the browser's ordinary ``input`` event.
    Selecting first makes that input replace existing text rather than append it.
    This deliberately rejects hidden overlays and non-text controls before any
    mutation is dispatched.
    """
    try:
        await _require_unobscured(tab, backend_node_id)
        remote = await tab.send(cdp.dom.resolve_node(backend_node_id=backend_node_id))
        if remote.object_id is None:
            return _failed("target_detached", "text control detached before it could be focused")
        result, exception = await tab.send(
            cdp.runtime.call_function_on(
                """function () {
                    const element = this;
                    const tag = (element.tagName || '').toLowerCase();
                    const type = (element.getAttribute('type') || '').toLowerCase();
                    const editable = element.isContentEditable === true;
                    const textInput = tag === 'textarea' ||
                      (tag === 'input' && !['button','checkbox','file','hidden','image',
                        'radio','range','reset','submit'].includes(type));
                    if (!editable && !textInput) return {ok: false, code: 'not_text_entry'};
                    if (element.disabled) return {ok: false, code: 'disabled'};
                    if (element.readOnly) return {ok: false, code: 'readonly'};
                    element.focus({preventScroll: true});
                    if (typeof element.select === 'function') {
                      element.select();
                    } else if (editable) {
                      const range = document.createRange();
                      range.selectNodeContents(element);
                      const selection = window.getSelection();
                      selection.removeAllRanges();
                      selection.addRange(range);
                    }
                    return {ok: true};
                }""",
                object_id=remote.object_id,
                return_by_value=True,
            )
        )
    except StaleTargetError as error:
        if "obstructed" in str(error):
            return _failed("target_obscured", "text control is obscured or moved")
        return _failed("target_detached", "text control detached before input could be dispatched")
    except Exception:
        return _failed("target_detached", "text control detached before input could be dispatched")
    finally:
        if "remote" in locals() and remote.object_id is not None:
            with suppress(Exception):
                await tab.send(cdp.runtime.release_object(remote.object_id))
    if exception is not None or not isinstance(result.value, dict):
        return _failed("evaluation_failed", "text control could not be prepared for input")
    if result.value.get("ok") is not True:
        code = result.value.get("code")
        if code in {"disabled", "readonly", "not_text_entry"}:
            return _failed(str(code), f"text control is {str(code).replace('_', ' ')}")
        return _failed("target_detached", "text control is no longer available")
    return ActionResult(OutcomeStatus.SUCCEEDED, "text control prepared")


async def type_text(
    tab: Tab,
    backend_node_id: BackendNodeId,
    text: str,
    *,
    submit: bool,
    timeouts: TimeoutConfig,
) -> ActionResult:
    prepared = await prepare_text_entry(tab, backend_node_id)
    if prepared.status is not OutcomeStatus.SUCCEEDED:
        return prepared
    try:
        await tab.send(cdp.input_.insert_text(text))
        if submit:
            await _press_enter(tab)
        else:
            # Browser-native blur emits ``change`` after Input.insertText's
            # regular ``input`` event. Do not synthesize either event directly.
            await _blur_active_element(tab)
        await asyncio.sleep(timeouts.settle)
    except Exception:
        return ActionResult(
            OutcomeStatus.UNCERTAIN,
            "text input may have been dispatched but completion could not be confirmed",
            error_code="post_dispatch_uncertain",
            retryable=False,
        )
    return ActionResult(OutcomeStatus.SUCCEEDED, "text input completed")


async def type_otp(
    tab: Tab,
    backend_node_id: BackendNodeId,
    code: str,
    timeouts: TimeoutConfig,
) -> ActionResult:
    prepared = await prepare_text_entry(tab, backend_node_id)
    if prepared.status is not OutcomeStatus.SUCCEEDED:
        return prepared
    try:
        # Do not resolve the later controls. Sites own OTP focus advancement,
        # including pasted-code handlers and single-character inputs.
        for character in code:
            await tab.send(cdp.input_.insert_text(character))
        await _blur_active_element(tab)
        await asyncio.sleep(timeouts.settle)
    except Exception:
        return ActionResult(
            OutcomeStatus.UNCERTAIN,
            "verification-code input may have been dispatched but completion could not be confirmed",
            error_code="post_dispatch_uncertain",
            retryable=False,
        )
    return ActionResult(
        OutcomeStatus.SUCCEEDED,
        "verification-code input completed",
        details={"characters_entered": len(code)},
    )


async def select_option(
    tab: Tab,
    backend_node_id: BackendNodeId,
    label: str,
    timeouts: TimeoutConfig,
) -> ActionResult:
    """Select only one option whose displayed label is an exact match."""
    try:
        await _require_unobscured(tab, backend_node_id)
        remote = await tab.send(cdp.dom.resolve_node(backend_node_id=backend_node_id))
        if remote.object_id is None:
            return _failed("target_detached", "select control detached before selection")
        result, exception = await tab.send(
            cdp.runtime.call_function_on(
                """function (label) {
                    if ((this.tagName || '').toLowerCase() !== 'select') {
                      return {ok: false, code: 'not_select'};
                    }
                    if (this.disabled) return {ok: false, code: 'disabled'};
                    const options = Array.from(this.options);
                    const option = options.find((item) => item.text === label);
                    if (!option) return {ok: false, code: 'option_not_found'};
                    this.selectedIndex = options.indexOf(option);
                    this.dispatchEvent(new Event('input', {bubbles: true}));
                    this.dispatchEvent(new Event('change', {bubbles: true}));
                    return {ok: true};
                }""",
                object_id=remote.object_id,
                arguments=[cdp.runtime.CallArgument(value=label)],
                return_by_value=True,
            )
        )
        if exception is not None or not isinstance(result.value, dict):
            return _failed("evaluation_failed", "select control could not be updated")
        if result.value.get("ok") is not True:
            code = str(result.value.get("code", "selection_failed"))
            return _failed(code, f"select control {code.replace('_', ' ')}")
        await asyncio.sleep(timeouts.settle)
    except StaleTargetError as error:
        if "obstructed" in str(error):
            return _failed("target_obscured", "select control is obscured or moved")
        return _failed("target_detached", "select control detached before selection")
    except Exception:
        return ActionResult(
            OutcomeStatus.UNCERTAIN,
            "option selection may have been dispatched but completion could not be confirmed",
            error_code="post_dispatch_uncertain",
            retryable=False,
        )
    finally:
        if "remote" in locals() and remote.object_id is not None:
            with suppress(Exception):
                await tab.send(cdp.runtime.release_object(remote.object_id))
    return ActionResult(OutcomeStatus.SUCCEEDED, "option selected")


async def _dispatch_click(tab: Tab, x: float, y: float, timeouts: TimeoutConfig) -> None:
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


async def _require_unobscured(tab: Tab, backend_node_id: BackendNodeId) -> None:
    """Check target hit testing before focus or select can mutate a hidden control."""
    await tab.send(cdp.dom.scroll_into_view_if_needed(backend_node_id=backend_node_id))
    box = await tab.send(cdp.dom.get_box_model(backend_node_id=backend_node_id))
    subtree = await tab.send(
        cdp.dom.describe_node(backend_node_id=backend_node_id, depth=-1, pierce=False)
    )
    x, y = quad_center(box.content)
    hit_backend_id, _frame_id, _node_id = await tab.send(
        # UA shadow roots for text inputs can own the hit-test result even
        # though they are not descendants returned by DOM.describeNode.
        cdp.dom.get_node_for_location(round(x), round(y), include_user_agent_shadow_dom=False)
    )
    if hit_backend_id not in _subtree_backend_ids(subtree):
        raise StaleTargetError("control was obstructed or moved before input could be dispatched")


async def _press_enter(tab: Tab) -> None:
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyDown", key="Enter", code="Enter", windows_virtual_key_code=13
        )
    )
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyUp", key="Enter", code="Enter", windows_virtual_key_code=13
        )
    )


async def _blur_active_element(tab: Tab) -> None:
    _result, _exception = await tab.send(
        cdp.runtime.evaluate("document.activeElement && document.activeElement.blur()")
    )


def _failed(code: str, message: str) -> ActionResult:
    return ActionResult(OutcomeStatus.FAILED, message, error_code=code, retryable=False)


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
