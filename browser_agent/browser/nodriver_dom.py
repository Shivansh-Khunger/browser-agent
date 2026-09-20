# pyright: reportMissingTypeStubs=false, reportPrivateUsage=false
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Build bounded, frame-flattened semantic observations from CDP AX and DOM data."""

from __future__ import annotations

import math
import struct
from base64 import b64decode
from dataclasses import dataclass, field, replace
from hashlib import sha256

from nodriver import cdp
from nodriver.cdp.accessibility import AXNode, AXNodeId
from nodriver.cdp.dom import BackendNodeId, Node
from nodriver.core.tab import Tab

from . import nodriver_semantics as semantics
from .geometry import quad_bounds
from .models import (
    BrowserEvaluationError,
    ContextNode,
    Observation,
    ObservationLimits,
    ScreenshotMetadata,
    SemanticControl,
    TargetHandle,
    UnsupportedRegion,
    Viewport,
)
from .nodriver_frames import FrameContext


@dataclass(frozen=True, slots=True)
class ControlTarget:
    session: Tab
    backend_node_id: BackendNodeId
    frame_id: str
    document_generation: int


@dataclass(frozen=True, slots=True)
class ObservationCapture:
    observation: Observation
    control_index: dict[str, ControlTarget]


@dataclass(frozen=True, slots=True)
class _ControlCandidate:
    order: int
    target: ControlTarget
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


@dataclass(slots=True)
class _BuildState:
    observation_id: str
    limits: ObservationLimits
    viewport: Viewport
    controls: list[_ControlCandidate] = field(default_factory=list)
    context: list[_ContextCandidate] = field(default_factory=list)
    unsupported: list[UnsupportedRegion] = field(default_factory=list)
    frame_generations: dict[str, int] = field(default_factory=dict)
    order: int = 0


async def capture_observation(
    tab: Tab,
    *,
    observation_id: str,
    active_target_id: str,
    frame_root: FrameContext,
    limits: ObservationLimits | None = None,
) -> ObservationCapture:
    limits = limits or ObservationLimits()
    target_info = await tab.send(cdp.target.get_target_info())
    viewport = await read_viewport(tab)

    state = _BuildState(observation_id, limits, viewport)
    await _capture_frame(frame_root, state, offset=(0.0, 0.0), main=True)
    screenshot: ScreenshotMetadata | None = None
    warnings: tuple[str, ...] = ()
    try:
        screenshot_data = await tab.send(
            cdp.page.capture_screenshot(
                format_="png", from_surface=True, capture_beyond_viewport=False
            )
        )
        screenshot_bytes = b64decode(screenshot_data, validate=True)
        pixel_width, pixel_height = _png_dimensions(screenshot_bytes)
    except Exception:
        warnings = ("screenshot_capture_failed",)
    else:
        digest = sha256(screenshot_bytes).hexdigest()[:24]
        screenshot = ScreenshotMetadata(
            f"shot-{digest}",
            observation_id,
            active_target_id,
            viewport,
            pixel_width,
            pixel_height,
        )
    selected_controls = sorted(
        sorted(state.controls, key=_retention_key)[: limits.controls], key=lambda item: item.order
    )
    selected_context = sorted(
        sorted(state.context, key=_retention_key)[: limits.context], key=lambda item: item.order
    )
    control_index: dict[str, ControlTarget] = {}
    materialized: list[SemanticControl] = []
    for index, candidate in enumerate(selected_controls, start=1):
        control_id = f"c{index}"
        control_index[control_id] = candidate.target
        materialized.append(
            replace(candidate.control, handle=TargetHandle(observation_id, control_id))
        )
    omitted = {
        kind: count
        for kind, count in (
            ("controls", len(state.controls) - len(selected_controls)),
            ("context", len(state.context) - len(selected_context)),
            ("unsupported_regions", max(0, len(state.unsupported) - limits.context)),
        )
        if count
    }
    content_truncated = any(
        item.name_truncated or item.description_truncated or item.value_truncated
        for item in materialized
    ) or any(item.node.truncated for item in selected_context)
    observation = Observation(
        observation_id=observation_id,
        active_target_id=active_target_id,
        url=target_info.url,
        title=target_info.title,
        document_generation=frame_root.document_generation,
        frame_generations=state.frame_generations,
        viewport=viewport,
        controls=tuple(materialized),
        context=tuple(item.node for item in selected_context),
        unsupported_regions=tuple(state.unsupported[: limits.context]),
        screenshot=screenshot,
        warnings=warnings,
        truncated=bool(omitted) or content_truncated,
        omitted_counts=omitted,
    )
    return ObservationCapture(observation, control_index)


async def read_viewport(tab: Tab) -> Viewport:
    layout_viewport, visual_viewport, *_ = await tab.send(cdp.page.get_layout_metrics())
    return Viewport(
        width=float(layout_viewport.client_width),
        height=float(layout_viewport.client_height),
        device_scale=visual_viewport.zoom or 1.0,
        offset_x=float(visual_viewport.offset_x),
        offset_y=float(visual_viewport.offset_y),
        scale=visual_viewport.scale,
        scroll_x=float(layout_viewport.page_x),
        scroll_y=float(layout_viewport.page_y),
    )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("screenshot is not a PNG image")
    return struct.unpack(">II", data[16:24])


async def _capture_frame(
    frame: FrameContext,
    state: _BuildState,
    *,
    offset: tuple[float, float],
    main: bool = False,
    region_bounds: tuple[float, float, float, float] | None = None,
) -> None:
    state.frame_generations[str(frame.frame_id)] = frame.document_generation
    try:
        await frame.session.send(cdp.dom.enable())
        await frame.session.send(cdp.accessibility.enable())
        dom_root = await frame.session.send(cdp.dom.get_document(depth=-1, pierce=True))
        ax_nodes = await frame.session.send(
            cdp.accessibility.get_full_ax_tree(
                frame_id=None if frame.session_root else frame.frame_id
            )
        )
    except Exception:
        if main:
            raise
        state.unsupported.append(
            UnsupportedRegion(
                reason="frame_capture_failed",
                frame_breadcrumb=frame.breadcrumb,
                origin=frame.origin,
                bounds=region_bounds,
            )
        )
        return
    dom_by_backend_id = semantics.index_dom(dom_root)
    ax_by_id = {node.node_id: node for node in ax_nodes}
    root_ax = next((node for node in ax_nodes if node.parent_id is None), None)
    if root_ax is None and ax_nodes:
        # OOPIF AX roots can retain an external parent ID from Chromium's
        # cross-target accessibility tree even though this session owns the subtree.
        root_ax = ax_nodes[0]
    if root_ax is None:
        if main:
            raise BrowserEvaluationError("main frame accessibility tree is empty")
        state.unsupported.append(
            UnsupportedRegion(
                reason="frame_capture_failed",
                frame_breadcrumb=frame.breadcrumb,
                origin=frame.origin,
                bounds=region_bounds,
            )
        )
        return
    children = {str(child.frame_id): child for child in frame.children}
    inserted: set[str] = set()

    async def visit(ax_node: AXNode, priority_ancestor: bool = False) -> None:
        backend_id = ax_node.backend_dom_node_id
        dom_node = dom_by_backend_id.get(backend_id) if backend_id else None
        role = semantics.ax_text(ax_node.role).casefold()
        properties = semantics.properties(ax_node)
        if (
            dom_node
            and dom_node.node_name.casefold() in {"canvas", "video"}
            and not semantics.is_control(role, dom_node, properties)
        ):
            state.unsupported.append(
                UnsupportedRegion(
                    reason="pixel_only",
                    frame_breadcrumb=frame.breadcrumb,
                    origin=frame.origin,
                    bounds=_translate(await _bounds(frame.session, backend_id), offset),
                )
            )
        if not ax_node.ignored:
            state.order += 1
            await _collect_node(
                frame, ax_node, dom_node, backend_id, priority_ancestor, offset, state, ax_by_id
            )
        child_id = str(dom_node.frame_id) if dom_node and dom_node.frame_id else None
        if child_id and child_id in children:
            child = children[child_id]
            owner_label = semantics.attribute(dom_node, "name") or semantics.attribute(
                dom_node, "id"
            )
            if owner_label and child.breadcrumb[-1].startswith("frame-"):
                child = child.with_breadcrumb((*frame.breadcrumb, owner_label))
            inserted.add(child_id)
            owner_bounds = await _bounds(frame.session, backend_id)
            global_owner = _translate(owner_bounds, offset)
            child_offset = offset
            if child.session_root and global_owner is not None:
                child_offset = (global_owner[0], global_owner[1])
            await _capture_frame(
                child,
                state,
                offset=child_offset,
                region_bounds=global_owner,
            )
        for child_ax_id in ax_node.child_ids or ():
            child_ax = ax_by_id.get(child_ax_id)
            if child_ax is not None:
                await visit(child_ax, priority_ancestor or semantics.is_modal(ax_node))

    await visit(root_ax)
    for child in frame.children:
        if str(child.frame_id) not in inserted:
            state.unsupported.append(
                UnsupportedRegion(
                    reason="frame_owner_unmapped",
                    frame_breadcrumb=child.breadcrumb,
                    origin=child.origin,
                )
            )
            await _capture_frame(child, state, offset=offset)


async def _collect_node(
    frame: FrameContext,
    ax_node: AXNode,
    dom_node: Node | None,
    backend_id: BackendNodeId | None,
    priority_ancestor: bool,
    offset: tuple[float, float],
    state: _BuildState,
    ax_by_id: dict[AXNodeId, AXNode],
) -> None:
    role = semantics.ax_text(ax_node.role).casefold()
    properties = semantics.properties(ax_node)
    priority = priority_ancestor or semantics.is_modal(ax_node)
    if semantics.is_control(role, dom_node, properties):
        if backend_id is None:
            state.unsupported.append(
                UnsupportedRegion("semantic_node_unmapped", frame.breadcrumb, frame.origin)
            )
            return
        control = await _build_control(
            frame.session,
            state.observation_id,
            backend_id,
            ax_node,
            dom_node,
            role,
            frame.breadcrumb,
            frame.origin,
            offset,
            state.viewport,
            state.limits,
        )
        focused_or_error = "focused" in control.states or any(
            item.startswith("invalid") for item in control.states
        )
        viewport_rank, distance = _location_rank(control.bounds, state.viewport)
        state.controls.append(
            _ControlCandidate(
                state.order,
                ControlTarget(
                    frame.session, backend_id, str(frame.frame_id), frame.document_generation
                ),
                control,
                0 if priority or focused_or_error else 1,
                viewport_rank,
                distance,
            )
        )
    elif role in semantics.CONTEXT_ROLES:
        text = (
            semantics.ax_text(ax_node.name)
            or semantics.ax_text(ax_node.value)
            or semantics.descendant_text(ax_node, ax_by_id)
        )
        if not text:
            return
        text, truncated = semantics.truncate(text, state.limits.context_text)
        bounds = _translate(await _bounds(frame.session, backend_id), offset)
        viewport_rank, distance = _location_rank(bounds, state.viewport)
        state.context.append(
            _ContextCandidate(
                state.order,
                ContextNode(
                    kind=role,
                    text=text,
                    frame_breadcrumb=frame.breadcrumb,
                    truncated=truncated,
                    frame_origin=frame.origin,
                ),
                0 if priority or role in {"alert", "alertdialog"} else 1,
                viewport_rank,
                distance,
            )
        )


async def _build_control(
    session: Tab,
    observation_id: str,
    backend_id: BackendNodeId,
    ax_node: AXNode,
    dom_node: Node | None,
    role: str,
    breadcrumb: tuple[str, ...],
    origin: str | None,
    offset: tuple[float, float],
    viewport: Viewport,
    limits: ObservationLimits,
) -> SemanticControl:
    states = semantics.state_strings(ax_node)
    tag = dom_node.node_name.casefold() if dom_node else ""
    input_type = semantics.attribute(dom_node, "type").casefold() if tag == "input" else ""
    raw_name, raw_description, raw_value = (
        semantics.ax_text(ax_node.name),
        semantics.ax_text(ax_node.description),
        semantics.ax_text(ax_node.value),
    )
    sensitive = semantics.is_sensitive(dom_node, input_type, raw_name, raw_description)
    name, name_cut = semantics.truncate(raw_name, limits.name)
    description, description_cut = semantics.truncate(raw_description, limits.description)
    value, value_cut = (None, False)
    if raw_value and not sensitive:
        value, value_cut = semantics.truncate(raw_value, limits.value)
    bounds = _translate(await _bounds(session, backend_id), offset)
    viewport_rank, _ = _location_rank(bounds, viewport)
    return SemanticControl(
        TargetHandle(observation_id, "pending"),
        role,
        name,
        description,
        value,
        bool(raw_value) if role in {"textbox", "searchbox", "combobox"} else None,
        states,
        tag,
        input_type,
        breadcrumb,
        viewport_rank == 0,
        bounds,
        "geometry_unavailable" if bounds is None else None,
        sensitive,
        name_cut,
        description_cut,
        value_cut,
        origin,
    )


def _retention_key(item: _ControlCandidate | _ContextCandidate) -> tuple[object, ...]:
    return (item.priority, item.viewport_rank, item.distance, item.order)


def _location_rank(
    bounds: tuple[float, float, float, float] | None, viewport: Viewport
) -> tuple[int, float]:
    if bounds is None:
        return 2, math.inf
    x, y, width, height = bounds
    right, bottom = x + width, y + height
    if right > 0 and bottom > 0 and x < viewport.width and y < viewport.height:
        return 0, 0.0
    return 1, math.hypot(
        max(0.0, -right, x - viewport.width), max(0.0, -bottom, y - viewport.height)
    )


def _translate(
    bounds: tuple[float, float, float, float] | None, offset: tuple[float, float]
) -> tuple[float, float, float, float] | None:
    if bounds is None:
        return None
    return (bounds[0] + offset[0], bounds[1] + offset[1], bounds[2], bounds[3])


async def _bounds(
    session: Tab, backend_id: BackendNodeId | None
) -> tuple[float, float, float, float] | None:
    if backend_id is None:
        return None
    try:
        return quad_bounds(
            (await session.send(cdp.dom.get_box_model(backend_node_id=backend_id))).content
        )
    except Exception:
        return None
