from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from browser_agent.browser import (
    BrowserAction,
    BrowserConfig,
    NodriverSession,
    Observation,
    ObservationLimits,
    OutcomeStatus,
    SemanticControl,
    TimeoutConfig,
)
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


def _control_named(observation: Observation, name: str) -> SemanticControl:
    return next(control for control in observation.controls if control.name.strip() == name)


def _status_text(observation: Observation) -> str:
    return next(node for node in observation.context if node.kind == "status").text
