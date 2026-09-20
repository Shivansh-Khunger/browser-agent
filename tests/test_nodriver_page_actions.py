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
    OutcomeStatus,
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
<div id="out" role="status">not clicked</div>
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
        BrowserConfig(headless=True, timeouts=TimeoutConfig(navigation=10.0)),
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

            button = next(
                control for control in nav_result.observation.controls if control.role == "button"
            )
            status_before = next(
                node for node in nav_result.observation.context if node.kind == "status"
            )
            assert status_before.text == "not clicked"

            click_result = await session.execute(BrowserAction("click", target=button.handle))
            assert click_result.status is OutcomeStatus.SUCCEEDED
            assert click_result.observation is not None
            status_after = next(
                node for node in click_result.observation.context if node.kind == "status"
            )
            assert status_after.text == "clicked"
    finally:
        await session.close()
