from __future__ import annotations

import pytest

from browser_agent.browser.models import ActionResult, OutcomeStatus
from browser_agent.state.adapter import BrowserStateAdapter
from browser_agent.state.artifacts import ArtifactStore
from browser_agent.state.models import (
    ActionRequest,
    ArtifactKind,
    CapturePolicy,
    EpisodeMetadata,
    EpisodeOutcome,
    SecurityClass,
)
from tests.fakes import FakeArtifactStore, FakeBrowserStateAdapter, observation


@pytest.mark.asyncio
async def test_artifact_store_is_content_addressed_and_round_trips_bytes() -> None:
    store = FakeArtifactStore()

    assert isinstance(store, ArtifactStore)

    first = await store.put(
        b'{"safe":true}',
        kind=ArtifactKind.ACTION,
        media_type="application/json",
        schema_version=1,
        security_class=SecurityClass.REDACTED,
        redaction_policy_version="redaction-v1",
    )
    second = await store.put(
        b'{"safe":true}',
        kind=ArtifactKind.ACTION,
        media_type="application/json",
        schema_version=1,
        security_class=SecurityClass.REDACTED,
        redaction_policy_version="redaction-v1",
    )

    assert first == second
    assert first.content_id.startswith("sha256:")
    assert await store.get(first) == b'{"safe":true}'


@pytest.mark.asyncio
async def test_state_adapter_records_action_and_returns_terminal_checkpoint() -> None:
    adapter = FakeBrowserStateAdapter()
    assert isinstance(adapter, BrowserStateAdapter)
    lease = await adapter.open_episode(
        seed_checkpoint=None,
        policy=CapturePolicy(version="capture-v1", redaction_policy_version="redaction-v1"),
        metadata=EpisodeMetadata(code_revision="abc123", platform="test"),
    )
    capture = await adapter.begin_action(
        lease,
        ActionRequest(task_id="task-1", action_id="action-1", name="navigate"),
    )
    delta = await adapter.finish_action(
        capture,
        ActionResult(OutcomeStatus.SUCCEEDED, "navigated"),
        observation("o1"),
    )
    checkpoint = await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)

    assert delta.episode_id == lease.episode_id
    assert delta.action_id == "action-1"
    assert delta.outcome is OutcomeStatus.SUCCEEDED
    assert checkpoint.parent_id is None
    assert checkpoint.clean_shutdown is True
    assert not hasattr(lease, "profile_path")
