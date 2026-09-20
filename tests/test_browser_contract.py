from __future__ import annotations

import pytest

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
from browser_agent.browser.transport import BrowserTransport
from tests.fakes import FakeBrowserTransport, observation


@pytest.mark.asyncio
async def test_browser_session_has_one_way_lifecycle_and_structured_results() -> None:
    session = FakeBrowserTransport(BrowserConfig())

    assert isinstance(session, BrowserTransport)

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
    session = FakeBrowserTransport(
        BrowserConfig(), observations=(observation("o1"), observation("o2"))
    )
    await session.start()
    await session.observe()
    await session.observe()

    with pytest.raises(StaleTargetError, match="belongs to observation o1"):
        await session.execute(BrowserAction("click", target=TargetHandle("o1", "control-1")))
