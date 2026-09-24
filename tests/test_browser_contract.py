from __future__ import annotations

import pytest

from browser_agent.browser import BrowserSession
from browser_agent.browser.models import (
    ActionResult,
    BrowserAction,
    BrowserConfig,
    OutcomeStatus,
    SessionLifecycle,
    SessionStateError,
    StaleTargetError,
    TargetHandle,
)
from tests.fakes import FakeBrowserSession, FakeBrowserTransport, observation


@pytest.mark.asyncio
async def test_browser_session_has_one_way_lifecycle_and_structured_results() -> None:
    transport = FakeBrowserTransport()
    session = FakeBrowserSession(BrowserConfig(), transport)

    assert isinstance(session, BrowserSession)

    assert session.lifecycle is SessionLifecycle.NEW

    await session.start()
    result = await session.execute(BrowserAction("navigate", {"url": "https://example.test"}))
    await session.close()
    await session.close()

    assert result == ActionResult(
        status=OutcomeStatus.SUCCEEDED,
        message="navigate completed",
    )
    assert session.lifecycle is SessionLifecycle.CLOSED

    with pytest.raises(SessionStateError, match="cannot start from closed"):
        await session.start()


@pytest.mark.asyncio
async def test_new_observation_invalidates_old_target_handles() -> None:
    transport = FakeBrowserTransport(observations=(observation("o1"), observation("o2")))
    session = FakeBrowserSession(BrowserConfig(), transport)
    await session.start()
    await session.observe()
    await session.observe()

    with pytest.raises(StaleTargetError, match="belongs to observation o1"):
        await session.execute(BrowserAction("click", target=TargetHandle("o1", "control-1")))


@pytest.mark.asyncio
async def test_start_failure_closes_session_and_cannot_retry() -> None:
    transport = FakeBrowserTransport(start_error=RuntimeError("launch failed"))
    session = FakeBrowserSession(BrowserConfig(), transport)

    with pytest.raises(RuntimeError, match="launch failed"):
        await session.start()

    assert session.lifecycle is SessionLifecycle.CLOSED
    with pytest.raises(SessionStateError, match="cannot start from closed"):
        await session.start()
