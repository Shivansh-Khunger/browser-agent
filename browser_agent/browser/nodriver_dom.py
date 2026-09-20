# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Flatten one main-frame CDP accessibility/DOM tree into an Observation."""

from __future__ import annotations

from dataclasses import dataclass

from nodriver import cdp
from nodriver.cdp.accessibility import AXNode, AXNodeId, AXPropertyName, AXValue
from nodriver.cdp.dom import BackendNodeId, Node
from nodriver.core.tab import Tab

from .geometry import quad_bounds
from .models import ContextNode, Observation, SemanticControl, TargetHandle, Viewport

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
    }
)

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
    }
)


@dataclass(frozen=True, slots=True)
class ObservationCapture:
    """An Observation plus its control_id -> backend_node_id index."""

    observation: Observation
    control_index: dict[str, BackendNodeId]


async def capture_observation(
    tab: Tab,
    *,
    observation_id: str,
    active_target_id: str,
    document_generation: int,
) -> ObservationCapture:
    await tab.send(cdp.dom.enable())
    await tab.send(cdp.accessibility.enable())
    root = await tab.send(cdp.dom.get_document(depth=-1, pierce=True))
    ax_nodes = await tab.send(cdp.accessibility.get_full_ax_tree())
    target_info = await tab.send(cdp.target.get_target_info())
    layout_viewport, visual_viewport, *_ = await tab.send(cdp.page.get_layout_metrics())

    dom_by_backend_id = _index_dom(root)
    ax_by_id = {node.node_id: node for node in ax_nodes}
    root_ax = next((node for node in ax_nodes if node.parent_id is None), None)

    controls: list[SemanticControl] = []
    context: list[ContextNode] = []
    control_index: dict[str, BackendNodeId] = {}
    counter = 0

    async def visit(ax_node: AXNode) -> None:
        nonlocal counter
        if not ax_node.ignored:
            role = _ax_text(ax_node.role)
            backend_node_id = ax_node.backend_dom_node_id
            dom_node = (
                dom_by_backend_id.get(backend_node_id) if backend_node_id is not None else None
            )
            if role in _INTERACTIVE_ROLES and backend_node_id is not None:
                counter += 1
                control_id = f"c{counter}"
                control_index[control_id] = backend_node_id
                controls.append(
                    await _build_control(
                        tab,
                        observation_id=observation_id,
                        control_id=control_id,
                        backend_node_id=backend_node_id,
                        ax_node=ax_node,
                        dom_node=dom_node,
                        role=role,
                    )
                )
            elif role in _CONTEXT_ROLES:
                text = (
                    _ax_text(ax_node.name)
                    or _ax_text(ax_node.value)
                    or _descendant_text(ax_node, ax_by_id)
                )
                if text:
                    context.append(ContextNode(kind=role, text=text))
        for child_id in ax_node.child_ids or ():
            child = ax_by_id.get(child_id)
            if child is not None:
                await visit(child)

    if root_ax is not None:
        await visit(root_ax)

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
    observation = Observation(
        observation_id=observation_id,
        active_target_id=active_target_id,
        url=target_info.url,
        title=target_info.title,
        document_generation=document_generation,
        frame_generations={"main": document_generation},
        viewport=viewport,
        controls=tuple(controls),
        context=tuple(context),
    )
    return ObservationCapture(observation, control_index)


def _descendant_text(ax_node: AXNode, ax_by_id: dict[AXNodeId, AXNode]) -> str:
    fragments: list[str] = []

    def walk(node: AXNode) -> None:
        if _ax_text(node.role) == "StaticText":
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
        # Same-frame only: iframe content_document subtrees are out of scope for
        # issue #12 (main frame only) and get_full_ax_tree() never crosses them.
        by_backend_id[node.backend_node_id] = node
        for child in node.children or ():
            walk(child)
        for shadow_root in node.shadow_roots or ():
            walk(shadow_root)

    walk(root)
    return by_backend_id


async def _build_control(
    tab: Tab,
    *,
    observation_id: str,
    control_id: str,
    backend_node_id: BackendNodeId,
    ax_node: AXNode,
    dom_node: Node | None,
    role: str,
) -> SemanticControl:
    states = {
        prop.name.value
        for prop in ax_node.properties or ()
        if prop.name in _STATE_PROPERTIES and bool(prop.value.value)
    }
    tag = dom_node.node_name.lower() if dom_node is not None else ""
    input_type = _attribute(dom_node, "type").lower() if tag == "input" else ""
    potentially_sensitive = input_type == "password"
    raw_value = None if potentially_sensitive else _ax_text(ax_node.value) or None
    bounds: tuple[float, float, float, float] | None = None
    visible = not any(
        prop.name is AXPropertyName.HIDDEN and bool(prop.value.value)
        for prop in ax_node.properties or ()
    )
    if visible:
        try:
            box = await tab.send(cdp.dom.get_box_model(backend_node_id=backend_node_id))
        except Exception:
            visible = False
        else:
            bounds = quad_bounds(box.content)
    return SemanticControl(
        handle=TargetHandle(observation_id, control_id),
        role=role,
        name=_ax_text(ax_node.name),
        description=_ax_text(ax_node.description),
        value=raw_value,
        filled=bool(raw_value) if role in {"textbox", "searchbox", "combobox"} else None,
        states=frozenset(states),
        tag=tag,
        input_type=input_type,
        visible=visible,
        bounds=bounds,
        potentially_sensitive=potentially_sensitive,
    )


def _attribute(node: Node | None, name: str) -> str:
    if node is None or not node.attributes:
        return ""
    pairs = node.attributes
    for index in range(0, len(pairs) - 1, 2):
        if pairs[index] == name:
            return pairs[index + 1]
    return ""


def _ax_text(value: AXValue | None) -> str:
    if value is None or value.value is None:
        return ""
    return str(value.value)
