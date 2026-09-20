from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from nodriver import cdp

from browser_agent.browser import (
    BrowserAction,
    BrowserConfig,
    NodriverSession,
    Observation,
    ObservationLimits,
    OutcomeStatus,
    SemanticControl,
    StaleTargetError,
    TimeoutConfig,
)
from browser_agent.browser.nodriver_frames import FrameContext
from browser_agent.state import (
    CapturePolicy,
    EpisodeMetadata,
    LocalArtifactStore,
    LocalBrowserStateAdapter,
)

_FIXTURE_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Local Fixture</title></head>
<body>
<h1>Local Fixture</h1>
<button id="go" onclick="document.getElementById('out').textContent = 'clicked'">
  Click me
</button>
<button id="nested" onclick="document.getElementById('out').textContent = 'nested clicked'">
  <span>Click <b>me</b> too</span>
</button>
<div id="out" role="status">not clicked</div>
<div role="alert">Invalid account details</div>
<button disabled>Unavailable</button>
<input aria-label="Read only field" value="ordinary-value" readonly>
<div role="button" tabindex="0">Focusable custom control</div>
<button hidden>Invisible control</button>
<button inert>Inert control</button>
<button aria-hidden="true">ARIA hidden control</button>
<label>Account password <input type="password" value="password-canary"></label>
<label>Verification code <input name="otp" value="otp-canary" autofocus></label>
<section aria-label="Open shadow host" id="shadow"></section>
<canvas aria-label="Pixel-only chart" width="40" height="20"
        onclick="document.getElementById('out').textContent = 'canvas clicked'"></canvas>
<script>
  const root = document.getElementById('shadow').attachShadow({mode: 'open'});
  root.innerHTML = '<button>Shadow action</button>';
</script>
</body>
</html>
"""


@contextmanager
def _local_fixture_server(directory: Path) -> Iterator[str]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(_FIXTURE_HTML)
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/index.html"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@contextmanager
def _frame_fixture_servers(directory: Path) -> Iterator[str]:
    child_directory = directory / "cross"
    child_directory.mkdir(parents=True, exist_ok=True)
    (child_directory / "cross.html").write_text(
        """<!doctype html><html><body>
<h2>Cross frame</h2>
<button onclick="document.getElementById('cross-out').textContent='cross clicked'">
  Cross action
</button>
<div id="cross-out" role="status">cross idle</div>
</body></html>"""
    )
    handler = partial(SimpleHTTPRequestHandler, directory=str(child_directory))
    child_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    child_thread = threading.Thread(target=child_server.serve_forever, daemon=True)
    child_thread.start()

    parent_directory = directory / "parent"
    parent_directory.mkdir(parents=True, exist_ok=True)
    (parent_directory / "same.html").write_text(
        """<!doctype html><html><body>
<h2>Same frame</h2>
<button onclick="document.getElementById('same-out').textContent='same clicked'">
  Same action
</button>
<div id="same-out" role="status">same idle</div>
<iframe name="nested-frame" src="/nested.html"></iframe>
</body></html>"""
    )
    (parent_directory / "nested.html").write_text(
        """<!doctype html><html><body>
<h3>Nested frame</h3>
<button onclick="document.getElementById('nested-out').textContent='nested clicked'">
  Nested action
</button>
<div id="nested-out" role="status">nested idle</div>
</body></html>"""
    )
    child_port = child_server.server_address[1]
    (parent_directory / "index.html").write_text(
        f"""<!doctype html><html><head><title>Frame Fixture</title></head><body>
<h1>Main before</h1>
<button>Main before action</button>
<iframe name="same-frame" src="/same.html"></iframe>
<iframe name="cross-frame" style="margin-left: 200px"
        src="http://localhost:{child_port}/cross.html"></iframe>
<button onclick="document.querySelector('[name=cross-frame]').remove()">Remove cross frame</button>
<button>Main after action</button>
</body></html>"""
    )
    parent_handler = partial(SimpleHTTPRequestHandler, directory=str(parent_directory))
    parent_server = ThreadingHTTPServer(("127.0.0.1", 0), parent_handler)
    parent_thread = threading.Thread(target=parent_server.serve_forever, daemon=True)
    parent_thread.start()
    try:
        yield f"http://127.0.0.1:{parent_server.server_address[1]}/index.html"
    finally:
        parent_server.shutdown()
        parent_thread.join()
        parent_server.server_close()
        child_server.shutdown()
        child_thread.join()
        child_server.server_close()


def _policy() -> CapturePolicy:
    return CapturePolicy("v1", "redaction-v1", restricted_storage=True)


def _episode_metadata() -> EpisodeMetadata:
    return EpisodeMetadata(code_revision="test", platform="replaced-at-launch")


@pytest.mark.asyncio
async def test_navigate_observe_click_changes_page_state(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(
        BrowserConfig(headless=True, timeouts=TimeoutConfig(navigation=10.0, settle=0.3)),
        state,
        _policy(),
        _episode_metadata(),
    )
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise

    try:
        with _local_fixture_server(tmp_path / "site") as url:
            nav_result = await session.execute(BrowserAction("navigate", {"url": url}))
            assert nav_result.status is OutcomeStatus.SUCCEEDED
            assert nav_result.observation is not None
            assert nav_result.observation.url == url
            assert nav_result.observation.title == "Local Fixture"

            button = _control_named(nav_result.observation, "Click me")
            assert _status_text(nav_result.observation) == "not clicked"

            click_result = await session.execute(BrowserAction("click", target=button.handle))
            assert click_result.status is OutcomeStatus.SUCCEEDED
            assert click_result.observation is not None
            assert _status_text(click_result.observation) == "clicked"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_click_lands_on_control_when_hit_test_resolves_a_child_element(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(
        BrowserConfig(headless=True, timeouts=TimeoutConfig(navigation=10.0, settle=0.3)),
        state,
        _policy(),
        _episode_metadata(),
    )
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise

    try:
        with _local_fixture_server(tmp_path / "site") as url:
            nav_result = await session.execute(BrowserAction("navigate", {"url": url}))
            assert nav_result.observation is not None

            # The click point hit-tests to the nested <b> text node, not the
            # <button> itself; the click must still succeed against the button's
            # own target handle rather than being rejected as obstructed/stale.
            button = _control_named(nav_result.observation, "Click me too")

            click_result = await session.execute(BrowserAction("click", target=button.handle))
            assert click_result.status is OutcomeStatus.SUCCEEDED
            assert click_result.observation is not None
            assert _status_text(click_result.observation) == "nested clicked"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_click_at_requires_current_bounded_screenshot_coordinates(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(
        BrowserConfig(headless=True, timeouts=TimeoutConfig(navigation=10.0, settle=0.1)),
        state,
        _policy(),
        _episode_metadata(),
    )
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise

    try:
        with _local_fixture_server(tmp_path / "site") as url:
            result = await session.execute(BrowserAction("navigate", {"url": url}))
            assert result.observation is not None
            observation = result.observation
            screenshot = observation.screenshot
            assert screenshot is not None
            assert screenshot.observation_id == observation.observation_id
            assert screenshot.active_target_id == observation.active_target_id
            assert screenshot.viewport == observation.viewport
            assert screenshot.pixel_width is not None and screenshot.pixel_width > 0
            assert screenshot.pixel_height is not None and screenshot.pixel_height > 0
            pixel_region = next(
                region
                for region in observation.unsupported_regions
                if region.reason == "pixel_only"
            )
            assert pixel_region.bounds is not None

            arguments = {
                "observation_id": observation.observation_id,
                "screenshot_id": screenshot.screenshot_id,
                "x": 1.0,
                "y": 1.0,
            }
            with pytest.raises(StaleTargetError, match="belongs to observation"):
                await session.execute(
                    BrowserAction("click_at", {**arguments, "observation_id": "stale"})
                )
            with pytest.raises(StaleTargetError, match="mismatched, or stale"):
                await session.execute(
                    BrowserAction("click_at", {**arguments, "screenshot_id": "stale"})
                )
            with pytest.raises(ValueError, match="finite number"):
                await session.execute(BrowserAction("click_at", {**arguments, "x": float("nan")}))
            with pytest.raises(ValueError, match="inside the screenshot viewport"):
                await session.execute(
                    BrowserAction("click_at", {**arguments, "x": screenshot.pixel_width})
                )

            assert pixel_region.bounds is not None
            left, top, right, bottom = pixel_region.bounds
            clicked = await session.execute(
                BrowserAction(
                    "click_at",
                    {
                        **arguments,
                        "x": (left + right)
                        / 2
                        * screenshot.pixel_width
                        / screenshot.viewport.width,
                        "y": (top + bottom)
                        / 2
                        * screenshot.pixel_height
                        / screenshot.viewport.height,
                    },
                )
            )
            assert clicked.observation is not None
            assert _status_text(clicked.observation) == "canvas clicked"

            refreshed_button = _control_named(clicked.observation, "Click me")
            runtime = session._runtime
            assert runtime is not None
            await runtime._browser.main_tab.send(  # pyright: ignore[reportPrivateUsage]
                cdp.runtime.evaluate(
                    """const blocker = document.createElement('div');
                    blocker.style = 'position:fixed;inset:0;z-index:9999';
                    document.body.appendChild(blocker);"""
                )
            )
            with pytest.raises(StaleTargetError, match="obstructed or moved"):
                await session.execute(BrowserAction("click", target=refreshed_button.handle))
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_observation_is_bounded_semantic_and_secret_safe(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(
        BrowserConfig(
            headless=True,
            observation_limits=ObservationLimits(
                controls=20,
                context=20,
                name=14,
                description=10,
                value=5,
                context_text=12,
            ),
            timeouts=TimeoutConfig(navigation=10.0, settle=0.3),
        ),
        state,
        _policy(),
        _episode_metadata(),
    )
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise

    try:
        with _local_fixture_server(tmp_path / "site") as url:
            result = await session.execute(BrowserAction("navigate", {"url": url}))
            assert result.observation is not None
            observation = result.observation

            disabled = _control_named(observation, "Unavailable")
            assert "disabled" in disabled.states
            readonly = _control_named(observation, "Read only fiel")
            assert readonly.value == "ordin"
            assert readonly.value_truncated is True
            assert "readonly" in readonly.states
            assert readonly.name_truncated is True
            assert _control_named(observation, "Focusable cust")
            assert _control_named(observation, "Shadow action")
            assert all(control.name != "Invisible control" for control in observation.controls)
            assert all(control.name != "Inert control" for control in observation.controls)
            assert all(control.name != "ARIA hidden control" for control in observation.controls)

            password = _control_named(observation, "Account passwo")
            otp = _control_named(observation, "Verification c")
            for sensitive in (password, otp):
                assert sensitive.potentially_sensitive is True
                assert sensitive.filled is True
                assert sensitive.value is None

            assert all("canary" not in (control.value or "") for control in observation.controls)
            assert any(node.kind == "heading" for node in observation.context)
            assert any(
                node.kind == "alert" and node.text == "Invalid acco" and node.truncated
                for node in observation.context
            )
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_observation_limits_retain_priority_then_restore_document_order(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(
        BrowserConfig(
            headless=True,
            observation_limits=ObservationLimits(controls=3, context=1),
            timeouts=TimeoutConfig(navigation=10.0, settle=0.3),
        ),
        state,
        _policy(),
        _episode_metadata(),
    )
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise

    try:
        with _local_fixture_server(tmp_path / "site") as url:
            result = await session.execute(BrowserAction("navigate", {"url": url}))
            assert result.observation is not None
            observation = result.observation

            assert observation.truncated is True
            assert observation.omitted_counts["controls"] > 0
            assert observation.omitted_counts["context"] > 0
            assert len(observation.controls) == 3
            assert [control.name.strip() for control in observation.controls] == [
                "Click me",
                "Click me too",
                "Verification code",
            ]
            assert observation.context[0].kind == "alert"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_observe_and_click_same_process_frame_and_oopif(tmp_path, monkeypatch) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    state = LocalBrowserStateAdapter(tmp_path / "state", store)
    session = NodriverSession(
        BrowserConfig(headless=True, timeouts=TimeoutConfig(navigation=10.0, settle=0.3)),
        state,
        _policy(),
        _episode_metadata(),
    )
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise

    try:
        with _frame_fixture_servers(tmp_path / "frames") as url:
            result = await session.execute(BrowserAction("navigate", {"url": url}))
            assert result.observation is not None
            observation = result.observation
            names = [control.name.strip() for control in observation.controls]
            assert names == [
                "Main before action",
                "Same action",
                "Nested action",
                "Cross action",
                "Remove cross frame",
                "Main after action",
            ], (names, observation.unsupported_regions, observation.frame_generations)
            assert len(observation.frame_generations) == 4
            assert _control_named(observation, "Main before action").frame_breadcrumb == ("main",)

            runtime = session._runtime
            assert runtime is not None
            registry = runtime._frames
            original_reconcile = registry.reconcile
            frame_root = await original_reconcile()

            async def reconcile_with_failed_oopif() -> FrameContext:
                return _fail_cross_frame(frame_root)

            monkeypatch.setattr(registry, "reconcile", reconcile_with_failed_oopif)
            partial = await session.observe()
            failed_region = next(
                region
                for region in partial.unsupported_regions
                if region.reason == "frame_capture_failed"
            )
            assert failed_region.origin is not None and failed_region.origin.startswith(
                "http://localhost:"
            )
            assert failed_region.bounds is not None
            assert _control_named(partial, "Same action")
            assert all(control.name.strip() != "Cross action" for control in partial.controls)

            monkeypatch.setattr(registry, "reconcile", original_reconcile)
            observation = await session.observe()
            same = _control_named(observation, "Same action")
            nested = _control_named(observation, "Nested action")
            cross = _control_named(observation, "Cross action")
            assert same.frame_breadcrumb == ("main", "same-frame")
            assert nested.frame_breadcrumb == ("main", "same-frame", "nested-frame")
            assert cross.frame_breadcrumb == ("main", "cross-frame")
            assert same.frame_origin is not None and same.frame_origin.startswith(
                "http://127.0.0.1:"
            )
            assert cross.frame_origin is not None and cross.frame_origin.startswith(
                "http://localhost:"
            )
            assert cross.bounds is not None and cross.bounds[0] > 200
            assert (
                runtime._control_index[same.handle.control_id].session is runtime._browser.main_tab
            )
            assert (
                runtime._control_index[cross.handle.control_id].session
                is not runtime._browser.main_tab
            )

            cross_result = await session.execute(BrowserAction("click", target=cross.handle))
            assert cross_result.observation is not None
            assert _breadcrumb_for_context_text(cross_result.observation, "cross clicked") == (
                "main",
                "cross-frame",
            )
            same = _control_named(cross_result.observation, "Same action")
            same_result = await session.execute(BrowserAction("click", target=same.handle))
            assert same_result.observation is not None
            assert _breadcrumb_for_context_text(same_result.observation, "same clicked") == (
                "main",
                "same-frame",
            )
            nested = _control_named(same_result.observation, "Nested action")
            nested_result = await session.execute(BrowserAction("click", target=nested.handle))
            assert nested_result.observation is not None
            assert _breadcrumb_for_context_text(nested_result.observation, "nested clicked") == (
                "main",
                "same-frame",
                "nested-frame",
            )

            stale_cross = _control_named(nested_result.observation, "Cross action")
            await runtime._browser.main_tab.send(  # pyright: ignore[reportPrivateUsage]
                cdp.runtime.evaluate("document.querySelector('[name=cross-frame]').remove()")
            )
            with pytest.raises(StaleTargetError, match="stale frame document"):
                await session.execute(BrowserAction("click", target=stale_cross.handle))

            removed = await session.observe()
            assert len(removed.frame_generations) == 3
            assert all(control.name.strip() != "Cross action" for control in removed.controls)
    finally:
        await session.close()


def _control_named(observation: Observation, name: str) -> SemanticControl:
    return next(control for control in observation.controls if control.name.strip() == name)


def _status_text(observation: Observation) -> str:
    return next(node for node in observation.context if node.kind == "status").text


def _breadcrumb_for_context_text(observation: Observation, text: str) -> tuple[str, ...]:
    return next(node.frame_breadcrumb for node in observation.context if node.text == text)


class _FailingFrameSession:
    async def send(self, command):
        del command
        raise RuntimeError("fixture frame detached")


def _fail_cross_frame(frame: FrameContext) -> FrameContext:
    children = tuple(_fail_cross_frame(child) for child in frame.children)
    if frame.origin and frame.origin.startswith("http://localhost:"):
        return replace(frame, session=_FailingFrameSession())  # type: ignore[arg-type]
    return replace(frame, children=children)
