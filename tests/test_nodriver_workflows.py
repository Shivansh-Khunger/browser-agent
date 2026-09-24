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

_HTML = """<!doctype html><html><body>
<input aria-label="Search" value="old" oninput="out.textContent='input:' + value"
 onchange="out.textContent += ':change'">
<input aria-label="First" onchange="document.getElementById('second').remove()">
<input id="second" aria-label="Second">
<input aria-label="Disabled" disabled>
<input aria-label="Readonly" readonly value="locked">
<input aria-label="OTP first" maxlength="1" oninput="document.getElementById('otp-second').focus()">
<input id="otp-second" aria-label="OTP second" maxlength="1">
<select aria-label="Colour" onchange="out.textContent='selected:' + value">
  <option value="r">Red</option><option value="b">Blue</option>
</select>
<div id="out" role="status">idle</div>
</body></html>"""


@contextmanager
def _site(directory: Path) -> Iterator[str]:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(_HTML)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(directory))
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
        BrowserConfig(headless=True, timeouts=TimeoutConfig(navigation=10.0, settle=0.1)),
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


@pytest.mark.asyncio
async def test_native_text_select_and_otp_workflows_do_not_echo_secrets(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await _start_or_skip(session)
    try:
        with _site(tmp_path / "site") as url:
            navigation = await session.execute(BrowserAction("navigate", {"url": url}))
            assert navigation.observation is not None

            secret = "secret-canary"
            typed = await session.execute(
                BrowserAction(
                    "type",
                    {"text": secret},
                    target=_control(navigation.observation, "Search").handle,
                )
            )
            assert typed.status is OutcomeStatus.SUCCEEDED
            assert secret not in repr(typed)
            assert typed.observation is not None
            assert _status(typed.observation) == "input:[REDACTED]:change"

            otp = await session.execute(
                BrowserAction(
                    "type_otp",
                    {"code": "otp-secret-canary"},
                    target=_control(typed.observation, "OTP first").handle,
                )
            )
            assert otp.status is OutcomeStatus.SUCCEEDED
            assert "otp-secret-canary" not in repr(otp)
            assert otp.details["characters_entered"] == 17
            assert otp.observation is not None

            selected = await session.execute(
                BrowserAction(
                    "select_option",
                    {"label": "Blue"},
                    target=_control(otp.observation, "Colour").handle,
                )
            )
            assert selected.status is OutcomeStatus.SUCCEEDED
            assert selected.observation is not None
            assert _status(selected.observation) == "selected:b"

            missing = await session.execute(
                BrowserAction(
                    "select_option",
                    {"label": "blue"},
                    target=_control(selected.observation, "Colour").handle,
                )
            )
            assert missing.status is OutcomeStatus.FAILED
            assert missing.error_code == "option_not_found"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_native_form_reports_completed_fields_then_stops_on_detach(tmp_path: Path) -> None:
    session = _session(tmp_path)
    await _start_or_skip(session)
    try:
        with _site(tmp_path / "site") as url:
            navigation = await session.execute(BrowserAction("navigate", {"url": url}))
            assert navigation.observation is not None
            first = _control(navigation.observation, "First")
            second = _control(navigation.observation, "Second")

            result = await session.execute(
                BrowserAction(
                    "fill_form",
                    {
                        "fields": [
                            {"target": first.handle, "text": "one"},
                            {"target": second.handle, "text": "two"},
                        ]
                    },
                )
            )
            assert result.status is OutcomeStatus.PARTIAL
            assert result.details == {
                "completed_fields": (first.handle.control_id,),
                "completed_count": 1,
            }
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_native_read_actions_are_read_only_and_field_failures_are_structured(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path)
    await _start_or_skip(session)
    try:
        with _site(tmp_path / "site") as url:
            navigation = await session.execute(BrowserAction("navigate", {"url": url}))
            assert navigation.observation is not None
            observation = navigation.observation

            for name in ("Disabled", "Readonly"):
                result = await session.execute(
                    BrowserAction(
                        "type", {"text": "nope"}, target=_control(observation, name).handle
                    )
                )
                assert result.status is OutcomeStatus.FAILED
                assert result.error_code == name.lower()
                assert result.observation is not None
                observation = result.observation

            text = await session.execute(BrowserAction("read_page", read_only=True))
            html = await session.execute(BrowserAction("get_html", read_only=True))
            viewport = await session.execute(BrowserAction("viewport", read_only=True))
            evaluation = await session.execute(
                BrowserAction("evaluate", {"expression": "document.title"}, read_only=True)
            )
            assert "idle" in text.details["text"]
            assert "secret-canary" not in html.details["html"]
            assert viewport.details["width"] > 0
            assert evaluation.details["value"] == ""
    finally:
        await session.close()


def _status(observation: Observation) -> str:
    return next(node.text for node in observation.context if node.kind == "status")
