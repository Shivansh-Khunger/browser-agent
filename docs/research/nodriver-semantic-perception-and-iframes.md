# Nodriver semantic perception and iframe actions

Research date: 2026-09-19

## Question

Using nodriver and Chromium DevTools Protocol (CDP) primary sources, which accessibility-tree, DOM, target, frame, and input primitives can support indexed semantic controls across same-origin and cross-origin frames, and where must screenshot/coordinate fallbacks remain?

## Decision

Build indexed perception as a **per-frame, per-CDP-session accessibility snapshot enriched with DOM metadata**. Do not use nodriver's text/CSS lookup output as the canonical index and do not treat frame origin as the routing rule.

Each indexed control should carry a short-lived locator record:

```text
control_index
target_id / session_id
frame_id
ax_node_id
backend_dom_node_id (when present)
role, name, description, value, states
DOM tag and selected attributes
content quad / visibility status (when available)
```

Same-process frames can be queried through the page target using their `frameId`. Out-of-process iframes (OOPIFs), normally used for cross-site isolation, must be queried and acted on through their attached iframe target/session. Rebuild affected locator records after document, frame, or target changes; never assume DOM or AX identifiers survive navigation.

## Why this works

### 1. Accessibility tree supplies semantic controls

Use `Accessibility.getFullAXTree(frameId=...)` to take a frame snapshot. `AXNode` exposes computed role, accessible name, description, value, properties, parent/child links, optional owning `frameId`, and optional `backendDOMNodeId`. That backend ID is the bridge from a semantic node to DOM inspection and action. `Accessibility.queryAXTree` is useful for targeted name/role lookup, but a full ordered snapshot is a better source for stable agent-facing indexes within one observation step. [`AXNode`, `getFullAXTree`, and `queryAXTree` protocol definitions](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Accessibility.pdl#L178-L297)

Call `Accessibility.enable` only while incremental AX identity/events are needed. CDP says enabling keeps `AXNodeId`s consistent between calls but may affect performance. A snapshot-per-agent-step design can instead enable, capture, and disable, or keep it enabled only for active sessions. [`Accessibility.enable` contract](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Accessibility.pdl#L208-L213)

Do not index every AX node. Exclude ignored nodes and non-actionable structural nodes unless they provide context. Preserve semantic state needed by tools: disabled, checked, selected, expanded, required, readonly, focusable, editable, and value bounds where supplied.

### 2. DOM supplies metadata, geometry, and actionable identity

For AX nodes with `backendDOMNodeId`, use `DOM.describeNode` to obtain tag/attributes and optionally pierce iframe and shadow-root subtrees. Use `DOM.resolveNode` only in the node's owning target/session when JavaScript-object access is required. `DOM.getDocument(depth=-1, pierce=true)` is valid for a complete DOM view; avoid deprecated `DOM.getFlattenedDocument`. [`describeNode`, `getDocument`, and deprecation of `getFlattenedDocument`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/DOM.pdl#L285-L413) [`resolveNode`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/DOM.pdl#L633-L657)

CDP DOM nodes expose both `frameId` on frame owners and `contentDocument` when available. These fields support in-process iframe traversal, including nodriver's existing `get_document(-1, True)` pattern. [`DOM.Node` frame fields](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/DOM.pdl#L119-L190) [nodriver iframe DOM traversal](https://github.com/ultrafunkamsterdam/nodriver/blob/a71cda374651d13815a42c5eeb61af04a711eaa7/nodriver/core/tab.py#L548-L574)

### 3. Frame tree plus target sessions covers in-process and out-of-process frames

Use `Page.getFrameTree` as the logical frame hierarchy. Frame records include `id`, optional `parentId`, URL, and security origin. [`Page.Frame`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Page.pdl#L268-L302) [`Page.getFrameTree`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Page.pdl#L852-L856)

Use `Target.setAutoAttach(autoAttach=true, waitForDebuggerOnStart=false, flatten=true)` and `Target.attachedToTarget` to acquire sessions for OOPIF targets. `TargetInfo` identifies iframe targets and provides `parentFrameId`; flat mode routes commands with `sessionId`. CDP warns auto-attach is for directly related targets and may need recursive setup on attached targets. [`TargetInfo`, `attachToTarget`, and flat sessions](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Target.pdl#L15-L87) [`Target.setAutoAttach`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Target.pdl#L229-L249)

Nodriver already attaches with flat sessions, enables auto-attach, and wraps attached `iframe` targets as `IFrame` connections. Reuse that transport, but maintain an explicit application registry mapping frame IDs to owning nodriver `Tab`/`IFrame` sessions. Nodriver's `get_frames()` explicitly says it may not return every iframe, so it must not be the sole frame inventory. [nodriver attachment handling](https://github.com/ultrafunkamsterdam/nodriver/blob/a71cda374651d13815a42c5eeb61af04a711eaa7/nodriver/core/connection.py#L288-L384) [nodriver `get_frames()` limitation](https://github.com/ultrafunkamsterdam/nodriver/blob/a71cda374651d13815a42c5eeb61af04a711eaa7/nodriver/core/tab.py#L180-L196) [nodriver `IFrame`](https://github.com/ultrafunkamsterdam/nodriver/blob/a71cda374651d13815a42c5eeb61af04a711eaa7/nodriver/core/tab.py#L2069-L2082)

This split is architectural, not merely same-origin-policy handling: Chromium can render a child frame in another process, and its browser process combines frame information across processes for accessibility while routing input to the intended renderer. [Chromium OOPIF architecture](https://www.chromium.org/developers/design-documents/oop-iframes/)

### 4. Actions use DOM identity first, native input second

For a selected indexed control, route every command through its recorded target/session:

1. Revalidate `backendDOMNodeId`; if missing or stale, refresh that frame snapshot and resolve the index again.
2. Use `DOM.scrollIntoViewIfNeeded` and `DOM.getContentQuads`. Quads are reported relative to the owning target's viewport. [`scrollIntoViewIfNeeded` and `getContentQuads`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/DOM.pdl#L305-L318)
3. For pointer actions, dispatch `Input.dispatchMouseEvent` at a safe point inside a visible quad. CDP defines coordinates in CSS pixels relative to that target's main-frame viewport. [`Input.dispatchMouseEvent`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Input.pdl#L164-L205)
4. For text, call `DOM.focus`, then `Input.insertText` or `Input.dispatchKeyEvent`. Use `DOM.setFileInputFiles` for file inputs. [`DOM.focus`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/DOM.pdl#L340-L348) [`Input.dispatchKeyEvent` and `insertText`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Input.pdl#L96-L146) [`DOM.setFileInputFiles`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/DOM.pdl#L681-L691)

Nodriver exposes the required low-level path through `Tab.send(cdp...)`. Its current `Element.click()` calls JavaScript `el.click()`, while `Element.mouse_click()` is documented in source as potentially unreliable. New action code should own the CDP sequence above instead of treating either helper as the contract. [nodriver custom CDP commands](https://ultrafunkamsterdam.github.io/nodriver/nodriver/classes/tab.html#custom-cdp-commands) [nodriver element positioning and mouse-click implementation](https://github.com/ultrafunkamsterdam/nodriver/blob/a71cda374651d13815a42c5eeb61af04a711eaa7/nodriver/core/element.py#L488-L543)

## Required fallback boundary

Keep screenshot plus coordinate action as an explicit fallback, not primary perception. It remains required when:

- Content has no useful AX/DOM representation: canvas/WebGL controls, streamed or raster UI, image-only maps, or pixels produced outside ordinary DOM semantics.
- An AX node is semantic but has no `backendDOMNodeId`, so it cannot be safely mapped to a DOM node for geometry/action.
- Browser verification or anti-bot UI is deliberately hidden from normal DOM control. Nodriver itself documents viewport template matching for verification checkboxes hidden through shadow roots or workers. [nodriver template-location boundary](https://ultrafunkamsterdam.github.io/nodriver/nodriver/classes/tab.html#await-tab-template-location-and-await-tab-verify-cf)
- A usable content quad cannot be obtained, or hit testing/overlays make semantic activation fail after refresh and retry.
- Human review is required, especially CAPTCHA handoff. Automation should present the current screenshot and stop; it should not claim semantic or coordinate reliability.

Use `Page.captureScreenshot` for pixels and `Input.dispatchMouseEvent` for coordinates. Store screenshot dimensions, viewport metrics, device scale, target/session, frame, and scroll state with any coordinate fallback so replay can detect mismatches. [`Page.captureScreenshot`](https://github.com/ChromeDevTools/devtools-protocol/blob/1cab90fb8d2066eb9f969da4f9b9bc7c50889af2/pdl/domains/Page.pdl#L583-L603)

## Implementation constraints exposed by research

- Treat indexes as observation-scoped handles. Navigation, DOM replacement, frame process swaps, and target detach invalidate them.
- Reconcile `Page.getFrameTree` with attached `TargetInfo` records. Do not infer session ownership only from URL origin.
- Preserve `(target/session, frameId, backendDOMNodeId)` together. A backend node ID without owning session is incomplete.
- Capture AX and DOM in each OOPIF's session; never attempt cross-origin JavaScript from parent session.
- Prefer native CDP input for user-like action. JavaScript `el.click()` may remain a targeted compatibility fallback, not default.
- Return an explicit unsupported/fallback reason when no semantic mapping exists. Never silently omit unreachable frames or visual-only controls.

## Answer in one line

Use AX-tree semantics joined to DOM backend IDs within an explicit frame-to-target-session registry; act through DOM geometry and CDP input in that same session, retaining screenshot/coordinate or human fallback only for pixel-only, unmappable, obscured, or intentionally protected controls.
