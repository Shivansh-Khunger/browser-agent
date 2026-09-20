# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Build one bounded main-frame semantic observation from CDP AX and DOM data."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

from nodriver import cdp
from nodriver.cdp.accessibility import AXNode, AXNodeId, AXPropertyName, AXValue
from nodriver.cdp.dom import BackendNodeId, Node
from nodriver.core.tab import Tab

from .geometry import quad_bounds
from .models import (
    ContextNode,
    Observation,
    ObservationLimits,
    SemanticControl,
    TargetHandle,
    UnsupportedRegion,
    Viewport,
)

_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "treeitem",
    }
)
_CONTEXT_ROLES = frozenset(
    {
        "heading",
        "status",
        "alert",
        "alertdialog",
        "navigation",
        "banner",
        "main",
        "contentinfo",
        "complementary",
        "region",
        "paragraph",
        "label",
        "labeltext",
    }
)
_NATIVE_CONTROL_TAGS = frozenset({"button", "input", "select", "textarea", "summary"})
_STATE_PROPERTIES = frozenset(
    {
        AXPropertyName.DISABLED,
        AXPropertyName.CHECKED,
        AXPropertyName.EXPANDED,
        AXPropertyName.SELECTED,
        AXPropertyName.PRESSED,
        AXPropertyName.REQUIRED,
        AXPropertyName.INVALID,
        AXPropertyName.MULTISELECTABLE,
        AXPropertyName.MULTILINE,
        AXPropertyName.READONLY,
        AXPropertyName.FOCUSED,
    }
)
_SECRET_HINT = re.compile(
    r"(?:password|passcode|passwd|one[-_ ]?time|otp|verification[-_ ]?code|"
    r"security[-_ ]?code|secret|token|cvv|cvc|card[-_ ]?number|credit[-_ ]?card|ssn)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ObservationCapture:
    """Observation plus selected control ID to backend-node identity."""

    observation: Observation
    control_index: dict[str, BackendNodeId]


@dataclass(frozen=True, slots=True)
class _ControlCandidate:
    order: int
    backend_node_id: BackendNodeId
    control: SemanticControl
    priority: int
    viewport_rank: int
    distance: float


@dataclass(frozen=True, slots=True)
class _ContextCandidate:
    order: int
    node: ContextNode
    priority: int
    viewport_rank: int
    distance: float


async def capture_observation(
    tab: Tab,
    *,
    observation_id: str,
    active_target_id: str,
    document_generation: int,
    limits: ObservationLimits | None = None,
) -> ObservationCapture:
    limits = limits or ObservationLimits()
    await tab.send(cdp.dom.enable())
    await tab.send(cdp.accessibility.enable())
    root = await tab.send(cdp.dom.get_document(depth=-1, pierce=True))
    ax_nodes = await tab.send(cdp.accessibility.get_full_ax_tree())
    target_info = await tab.send(cdp.target.get_target_info())
    layout_viewport, visual_viewport, *_ = await tab.send(cdp.page.get_layout_metrics())

    viewport = Viewport(
        width=float(layout_viewport.client_width),
        height=float(layout_viewport.client_height),
        device_scale=visual_viewport.zoom or 1.0,
        offset_x=float(visual_viewport.offset_x),
        offset_y=float(visual_viewport.offset_y),
        scale=visual_viewport.scale,
        scroll_x=float(layout_viewport.page_x),
        scroll_y=float(layout_viewport.page_y),
    )
    dom_by_backend_id = _index_dom(root)
    ax_by_id = {node.node_id: node for node in ax_nodes}
    root_ax = next((node for node in ax_nodes if node.parent_id is None), None)
    controls: list[_ControlCandidate] = []
    context: list[_ContextCandidate] = []
    unsupported: list[UnsupportedRegion] = []
    order = 0

    async def visit(ax_node: AXNode, priority_ancestor: bool = False) -> None:
        nonlocal order
        if ax_node.ignored:
            for child_id in ax_node.child_ids or ():
                child = ax_by_id.get(child_id)
                if child is not None:
                    await visit(child, priority_ancestor)
            return
        order += 1
        node_order = order
        role = _ax_text(ax_node.role).casefold()
        properties = _properties(ax_node)
        backend_node_id = ax_node.backend_dom_node_id
        dom_node = dom_by_backend_id.get(backend_node_id) if backend_node_id else None
        modal = role in {"dialog", "alertdialog"} or _truthy(properties.get("modal"))
        priority = priority_ancestor or modal
        control_like = _is_control(role, dom_node, properties)

        if control_like and backend_node_id is None:
            unsupported.append(UnsupportedRegion(reason="semantic_node_unmapped"))
        elif control_like and backend_node_id is not None:
            control = await _build_control(
                tab,
                observation_id=observation_id,
                backend_node_id=backend_node_id,
                ax_node=ax_node,
                dom_node=dom_node,
                role=role,
                viewport=viewport,
                limits=limits,
            )
            focused_or_error = "focused" in control.states or any(
                state.startswith("invalid") for state in control.states
            )
            viewport_rank, distance = _location_rank(control.bounds, viewport)
            controls.append(
                _ControlCandidate(
                    node_order,
                    backend_node_id,
                    control,
                    0 if priority or focused_or_error else 1,
                    viewport_rank,
                    distance,
                )
            )
        elif role in _CONTEXT_ROLES:
            text = (
                _ax_text(ax_node.name)
                or _ax_text(ax_node.value)
                or _descendant_text(ax_node, ax_by_id)
            )
            if text:
                text, truncated = _truncate(text, limits.context_text)
                bounds = await _bounds(tab, backend_node_id) if backend_node_id else None
                viewport_rank, distance = _location_rank(bounds, viewport)
                context.append(
                    _ContextCandidate(
                        node_order,
                        ContextNode(kind=role, text=text, truncated=truncated),
                        0 if priority or role in {"alert", "alertdialog"} else 1,
                        viewport_rank,
                        distance,
                    )
                )

        for child_id in ax_node.child_ids or ():
            child = ax_by_id.get(child_id)
            if child is not None:
                await visit(child, priority or modal)

    if root_ax is not None:
        await visit(root_ax)

    selected_controls = sorted(
        sorted(controls, key=_retention_key)[: limits.controls],
        key=lambda item: item.order,
    )
    selected_context = sorted(
        sorted(context, key=_retention_key)[: limits.context],
        key=lambda item: item.order,
    )
    control_index: dict[str, BackendNodeId] = {}
    materialized_controls: list[SemanticControl] = []
    for index, candidate in enumerate(selected_controls, start=1):
        control_id = f"c{index}"
        control_index[control_id] = candidate.backend_node_id
        materialized_controls.append(
            replace(candidate.control, handle=TargetHandle(observation_id, control_id))
        )

    omitted_counts = {
        kind: count
        for kind, count in (
            ("controls", len(controls) - len(selected_controls)),
            ("context", len(context) - len(selected_context)),
            ("unsupported_regions", max(0, len(unsupported) - limits.context)),
        )
        if count
    }
    selected_unsupported = unsupported[: limits.context]
    content_truncated = any(
        control.name_truncated or control.description_truncated or control.value_truncated
        for control in materialized_controls
    ) or any(candidate.node.truncated for candidate in selected_context)
    observation = Observation(
        observation_id=observation_id,
        active_target_id=active_target_id,
        url=target_info.url,
        title=target_info.title,
        document_generation=document_generation,
        frame_generations={"main": document_generation},
        viewport=viewport,
        controls=tuple(materialized_controls),
        context=tuple(candidate.node for candidate in selected_context),
        unsupported_regions=tuple(selected_unsupported),
        truncated=bool(omitted_counts) or content_truncated,
        omitted_counts=omitted_counts,
    )
    return ObservationCapture(observation, control_index)


def _retention_key(candidate: _ControlCandidate | _ContextCandidate) -> tuple[object, ...]:
    return (candidate.priority, candidate.viewport_rank, candidate.distance, candidate.order)


def _location_rank(
    bounds: tuple[float, float, float, float] | None, viewport: Viewport
) -> tuple[int, float]:
    if bounds is None:
        return 2, math.inf
    x, y, width, height = bounds
    right = x + width
    bottom = y + height
    visible = right > 0 and bottom > 0 and x < viewport.width and y < viewport.height
    if visible:
        return 0, 0.0
    dx = max(0.0, -right, x - viewport.width)
    dy = max(0.0, -bottom, y - viewport.height)
    return 1, math.hypot(dx, dy)


def _descendant_text(ax_node: AXNode, ax_by_id: dict[AXNodeId, AXNode]) -> str:
    fragments: list[str] = []

    def walk(node: AXNode) -> None:
        if _ax_text(node.role).casefold() == "statictext":
            text = _ax_text(node.name)
            if text:
                fragments.append(text)
        for child_id in node.child_ids or ():
            child = ax_by_id.get(child_id)
            if child is not None:
                walk(child)

    for child_id in ax_node.child_ids or ():
        child = ax_by_id.get(child_id)
        if child is not None:
            walk(child)
    return " ".join(fragments)


def _index_dom(root: Node) -> dict[BackendNodeId, Node]:
    by_backend_id: dict[BackendNodeId, Node] = {}

    def walk(node: Node) -> None:
        by_backend_id[node.backend_node_id] = node
        for child in node.children or ():
            walk(child)
        for shadow_root in node.shadow_roots or ():
            walk(shadow_root)

    walk(root)
    return by_backend_id


def _is_control(role: str, node: Node | None, properties: dict[str, object]) -> bool:
    if role in _INTERACTIVE_ROLES or _truthy(properties.get("editable")):
        return True
    if node is None:
        return False
    tag = node.node_name.casefold()
    if _truthy(properties.get("focusable")):
        structural = {"rootwebarea", "webarea", "document", "iframe", "none", "presentation"}
        if role not in structural or _attribute(node, "tabindex"):
            return True
    if tag in _NATIVE_CONTROL_TAGS:
        return tag != "input" or _attribute(node, "type").casefold() != "hidden"
    return tag == "a" and bool(_attribute(node, "href"))


async def _build_control(
    tab: Tab,
    *,
    observation_id: str,
    backend_node_id: BackendNodeId,
    ax_node: AXNode,
    dom_node: Node | None,
    role: str,
    viewport: Viewport,
    limits: ObservationLimits,
) -> SemanticControl:
    states = _state_strings(ax_node)
    tag = dom_node.node_name.casefold() if dom_node is not None else ""
    input_type = _attribute(dom_node, "type").casefold() if tag == "input" else ""
    raw_name = _ax_text(ax_node.name)
    raw_description = _ax_text(ax_node.description)
    raw_value = _ax_text(ax_node.value)
    potentially_sensitive = _is_sensitive(dom_node, input_type, raw_name, raw_description)
    name, name_truncated = _truncate(raw_name, limits.name)
    description, description_truncated = _truncate(raw_description, limits.description)
    value: str | None = None
    value_truncated = False
    if raw_value and not potentially_sensitive:
        value, value_truncated = _truncate(raw_value, limits.value)
    bounds = await _bounds(tab, backend_node_id)
    viewport_rank, _distance = _location_rank(bounds, viewport)
    return SemanticControl(
        handle=TargetHandle(observation_id, "pending"),
        role=role,
        name=name,
        description=description,
        value=value,
        filled=bool(raw_value) if role in {"textbox", "searchbox", "combobox"} else None,
        states=states,
        tag=tag,
        input_type=input_type,
        visible=viewport_rank == 0,
        bounds=bounds,
        fallback_reason="geometry_unavailable" if bounds is None else None,
        potentially_sensitive=potentially_sensitive,
        name_truncated=name_truncated,
        description_truncated=description_truncated,
        value_truncated=value_truncated,
    )


async def _bounds(
    tab: Tab, backend_node_id: BackendNodeId | None
) -> tuple[float, float, float, float] | None:
    if backend_node_id is None:
        return None
    try:
        box = await tab.send(cdp.dom.get_box_model(backend_node_id=backend_node_id))
    except Exception:
        return None
    return quad_bounds(box.content)


def _properties(ax_node: AXNode) -> dict[str, object]:
    return {prop.name.value: prop.value.value for prop in ax_node.properties or ()}


def _state_strings(ax_node: AXNode) -> frozenset[str]:
    states: set[str] = set()
    for prop in ax_node.properties or ():
        if prop.name not in _STATE_PROPERTIES:
            continue
        value = prop.value.value
        if value is True or (isinstance(value, str) and value.casefold() == "true"):
            states.add(prop.name.value)
        elif value not in (None, False, "false"):
            states.add(f"{prop.name.value}:{str(value).casefold()}")
    return frozenset(states)


def _is_sensitive(node: Node | None, input_type: str, name: str, description: str) -> bool:
    if input_type == "password":
        return True
    hints = " ".join(
        (
            name,
            description,
            _attribute(node, "name"),
            _attribute(node, "id"),
            _attribute(node, "autocomplete"),
            _attribute(node, "aria-label"),
            _attribute(node, "placeholder"),
        )
    )
    return bool(_SECRET_HINT.search(hints))


def _attribute(node: Node | None, name: str) -> str:
    if node is None or not node.attributes:
        return ""
    pairs = node.attributes
    for index in range(0, len(pairs) - 1, 2):
        if pairs[index].casefold() == name.casefold():
            return pairs[index + 1]
    return ""


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.casefold() not in {"", "false", "none", "undefined"}
    return bool(value)


def _ax_text(value: AXValue | None) -> str:
    if value is None or value.value is None:
        return ""
    return str(value.value)
