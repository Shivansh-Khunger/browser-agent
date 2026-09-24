from __future__ import annotations

import threading
import time
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


class _FixtureHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/slow":
            time.sleep(1.0)
            body = b"done"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


@contextmanager
def _site(directory: Path) -> Iterator[str]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(
        """<!doctype html><html><head><title>Launch</title></head><body>
<button onclick="window.open('/child.html?name=one', '_blank')">Open window child</button>
<a href="/child.html?name=anchor" target="_blank" rel="noopener">Open anchor child</a>
<button onclick="window.open('/child.html?name=left'); window.open('/child.html?name=right')">
  Open two children
</button>
<button onclick="document.getElementById('spa').textContent='rerendered'">SPA rerender</button>
<button onclick="fetch('/slow'); document.getElementById('spa').textContent='polling'">
  Start long poll
</button>
<div id="spa" role="status">initial</div>
</body></html>"""
    )
    (directory / "child.html").write_text(
        """<!doctype html><html><head><title>Child</title></head><body>
<h1>Child tab</h1>
<button onclick="window.open('/child.html?name=next', '_blank')">Open next child</button>
<button onclick="window.opener.close()">Close opener</button>
<button onclick="window.close()">Close self</button>
</body></html>"""
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_FixtureHandler, directory=str(directory))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _session(tmp_path: Path) -> NodriverSession:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    return NodriverSession(
        BrowserConfig(
            headless=True,
            timeouts=TimeoutConfig(navigation=10.0, action=5.0, settle=0.15),
        ),
        LocalBrowserStateAdapter(tmp_path / "state", store),
        CapturePolicy("v1", "redaction-v1", restricted_storage=True),
        EpisodeMetadata(code_revision="test", platform="replaced-at-launch"),
    )


async def _start_or_skip(session: NodriverSession) -> None:
    try:
        await session.start()
    except Exception as error:
        if "No installed Chrome or Chromium" in str(error):
            pytest.skip(str(error))
        raise


def _control(observation: Observation, name: str) -> SemanticControl:
    return next(control for control in observation.controls if control.name.strip() == name)


async def _click(session: NodriverSession, observation: Observation, name: str):
    return await session.execute(BrowserAction("click", target=_control(observation, name).handle))


@pytest.mark.asyncio
@pytest.mark.parametrize("control_name", ["Open window child", "Open anchor child"])
async def test_single_popup_variants_are_adopted_and_activated(
    tmp_path: Path, control_name: str
) -> None:
    session = _session(tmp_path)
    await _start_or_skip(session)
    try:
        with _site(tmp_path / "site") as url:
            launched = await session.execute(BrowserAction("navigate", {"url": url}))
            assert launched.observation is not None
            launch_target_id = launched.observation.active_target_id

            opened = await _click(session, launched.observation, control_name)

            assert opened.status is OutcomeStatus.SUCCEEDED
            assert opened.observation is not None
            assert opened.observation.title == "Child"
            assert opened.observation.active_target_id != launch_target_id
            assert session.active_target_id == opened.observation.active_target_id
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_multiple_popups_report_ambiguity_and_retain_active_tab(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await _start_or_skip(session)
    try:
        with _site(tmp_path / "site") as url:
            launched = await session.execute(BrowserAction("navigate", {"url": url}))
            assert launched.observation is not None
            launch_target_id = launched.observation.active_target_id

            result = await _click(session, launched.observation, "Open two children")

            assert result.status is OutcomeStatus.UNCERTAIN
            assert result.error_code == "ambiguous_target"
            assert result.retryable is False
            assert result.details["candidate_count"] == 2
            assert result.observation is not None
            assert result.observation.active_target_id == launch_target_id
            assert result.observation.title == "Launch"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_closed_active_tab_falls_back_through_opener_recent_adoption_and_launch(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    await _start_or_skip(session)
    try:
        with _site(tmp_path / "site") as url:
            launch = await session.execute(BrowserAction("navigate", {"url": url}))
            assert launch.observation is not None
            launch_target_id = launch.observation.active_target_id

            first = await _click(session, launch.observation, "Open window child")
            assert first.observation is not None
            first_target_id = first.observation.active_target_id
            second = await _click(session, first.observation, "Open next child")
            assert second.observation is not None
            second_target_id = second.observation.active_target_id
            third = await _click(session, second.observation, "Open next child")
            assert third.observation is not None
            assert third.observation.active_target_id not in {
                launch_target_id,
                first_target_id,
                second_target_id,
            }

            opener_closed = await _click(session, third.observation, "Close opener")
            assert opener_closed.observation is not None
            assert opener_closed.observation.active_target_id == third.observation.active_target_id

            recent_fallback = await _click(session, opener_closed.observation, "Close self")
            assert recent_fallback.observation is not None
            assert recent_fallback.observation.active_target_id == first_target_id

            launch_fallback = await _click(session, recent_fallback.observation, "Close self")
            assert launch_fallback.observation is not None
            assert launch_fallback.observation.active_target_id == launch_target_id
            assert launch_fallback.observation.title == "Launch"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_spa_rerender_and_long_poll_do_not_change_target_or_wait_for_network_idle(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    await _start_or_skip(session)
    try:
        with _site(tmp_path / "site") as url:
            launch = await session.execute(BrowserAction("navigate", {"url": url}))
            assert launch.observation is not None
            target_id = launch.observation.active_target_id

            rerendered = await _click(session, launch.observation, "SPA rerender")
            assert rerendered.observation is not None
            assert rerendered.observation.active_target_id == target_id

            started = time.monotonic()
            polling = await _click(session, rerendered.observation, "Start long poll")
            elapsed = time.monotonic() - started

            assert polling.status is OutcomeStatus.SUCCEEDED
            assert polling.observation is not None
            assert polling.observation.active_target_id == target_id
            assert elapsed < 0.8
    finally:
        await session.close()
