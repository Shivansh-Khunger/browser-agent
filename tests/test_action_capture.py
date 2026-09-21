from __future__ import annotations

import json
from dataclasses import replace

import pytest

from browser_agent.browser.models import (
    ActionResult,
    BrowserAction,
    BrowserEvidence,
    ContextNode,
    CookieChange,
    NetworkEvent,
    OutcomeStatus,
    StorageEvent,
    TargetHandle,
)
from browser_agent.state.artifacts import LocalArtifactStore
from browser_agent.state.capture import redact_action_input
from browser_agent.state.local import LocalBrowserStateAdapter
from browser_agent.state.models import (
    ActionEvidence,
    ActionRequest,
    ArtifactKind,
    CapturePolicy,
    EpisodeMetadata,
)
from tests.fakes import observation


def test_action_input_redaction_preserves_shape_not_secret_values() -> None:
    secret = "secret-canary"
    target = TargetHandle("o1", "c1")

    typed = redact_action_input(BrowserAction("type", {"text": secret}, target=target))
    form = redact_action_input(
        BrowserAction(
            "fill_form",
            {"fields": [{"target": target, "text": secret}], "submit": True},
        )
    )

    assert secret not in repr(typed)
    assert typed["text"] == {
        "placeholder": "[REDACTED]",
        "character_count": len(secret),
        "sensitivity": "type",
    }
    assert secret not in repr(form)
    assert form["fields"] == [
        {
            "target": str(target),
            "character_count": len(secret),
            "placeholder": "[REDACTED]",
            "sensitivity": "form_value",
        }
    ]
    assert form["submit"] is True


@pytest.mark.asyncio
async def test_action_record_moves_from_incomplete_to_atomic_redacted_commit(tmp_path) -> None:
    secret = "secret-canary"
    store = LocalArtifactStore(tmp_path / "artifacts")
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    lease = await adapter.open_episode(
        None,
        CapturePolicy("capture-v1", "redaction-v1", max_queue_items=8),
        EpisodeMetadata(code_revision="test", platform="test", task_id="task-1"),
    )
    action = BrowserAction("type", {"text": secret}, target=TargetHandle("o1", "c1"))
    capture = await adapter.begin_action(
        lease,
        ActionRequest(
            task_id="task-1",
            action_id="action-1",
            name=action.name,
            redacted_input=redact_action_input(action),
            target=action.target,
            pre_observation_id="o1",
            pre_observation_digest="sha256:before",
            pre_target_id="target-1",
            pre_url=f"https://example.test/?token={secret}",
        ),
    )
    pointer = tmp_path / "state" / "actions" / lease.episode_id / f"{capture.capture_id}.json"
    assert json.loads(pointer.read_text())["complete"] is False

    delta = await adapter.finish_action(
        capture,
        ActionResult(
            OutcomeStatus.SUCCEEDED,
            "typed",
            details={"text": secret, "characters_entered": len(secret)},
        ),
        observation("o2"),
        ActionEvidence(
            BrowserEvidence(
                network_events=(
                    NetworkEvent(
                        1,
                        "request",
                        "request-1",
                        1.0,
                        url=f"https://example.test/?token={secret}",
                        method="POST",
                        headers={"Authorization": secret, "x-safe": "yes"},
                    ),
                ),
                storage_events=(
                    StorageEvent(2, "updated", "local", "https://example.test", "api_token"),
                ),
                cookie_changes=(CookieChange("updated", "sid", "example.test", "/"),),
            )
        ),
    )

    committed = json.loads(pointer.read_text())
    artifact = await store.get(delta.record)  # type: ignore[arg-type]
    network = (tmp_path / "state" / "network" / f"{lease.episode_id}.jsonl").read_bytes()
    assert committed["complete"] is True
    assert delta.pre_observation_id == "o1"
    assert delta.post_observation_id == "o2"
    assert delta.network_span == (1, 1)
    assert delta.cookie_changes[0].name == "sid"
    assert secret.encode() not in artifact
    assert secret.encode() not in network
    assert b"authorization" not in network.lower()
    assert b"%5BREDACTED%5D" in network
    assert b"[REDACTED]" in artifact


@pytest.mark.asyncio
async def test_optional_event_overflow_drops_oldest_and_reports_omission(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    lease = await adapter.open_episode(
        None,
        CapturePolicy("capture-v1", "redaction-v1", max_queue_items=1),
        EpisodeMetadata(code_revision="test", platform="test"),
    )
    capture = await adapter.begin_action(lease, ActionRequest("task-1", "action-1", "navigate"))
    delta = await adapter.finish_action(
        capture,
        ActionResult(OutcomeStatus.SUCCEEDED, "done"),
        observation("o1"),
        ActionEvidence(
            BrowserEvidence(
                network_events=(
                    NetworkEvent(1, "request", "r1", 1.0),
                    NetworkEvent(2, "request", "r2", 2.0),
                )
            )
        ),
    )

    assert [event.request_id for event in delta.network_events] == ["r2"]
    assert "capacity_dropped" in delta.warnings
    assert "capacity_dropped:1" in delta.omissions


@pytest.mark.asyncio
async def test_optional_artifacts_are_redacted_or_explicitly_omitted(tmp_path) -> None:
    secret = "secret-canary"
    store = LocalArtifactStore(tmp_path / "artifacts")
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    lease = await adapter.open_episode(
        None,
        CapturePolicy(
            "capture-v1",
            "redaction-v1",
            optional_artifacts=frozenset(
                {ArtifactKind.OBSERVATION, ArtifactKind.DOM, ArtifactKind.SCREENSHOT}
            ),
        ),
        EpisodeMetadata(code_revision="test", platform="test"),
    )
    capture = await adapter.begin_action(
        lease, ActionRequest("task-1", "action-1", "read_page", read_only=True)
    )
    observed = replace(
        observation("o1"),
        title=secret,
        context=(ContextNode("status", secret),),
    )

    delta = await adapter.finish_action(
        capture,
        ActionResult(OutcomeStatus.SUCCEEDED, "done", details={"text": secret}),
        observed,
    )

    observation_ref = next(
        artifact for artifact in delta.artifacts if artifact.kind is ArtifactKind.OBSERVATION
    )
    assert secret.encode() not in await store.get(observation_ref)
    assert "dom:redaction_unavailable" in delta.omissions
    assert "screenshot:redaction_unavailable" in delta.omissions


@pytest.mark.asyncio
async def test_required_record_byte_overflow_stays_explicitly_incomplete(tmp_path) -> None:
    adapter = LocalBrowserStateAdapter(
        tmp_path / "state", LocalArtifactStore(tmp_path / "artifacts")
    )
    lease = await adapter.open_episode(
        None,
        CapturePolicy("capture-v1", "redaction-v1", max_artifact_bytes=16),
        EpisodeMetadata(code_revision="test", platform="test"),
    )
    capture = await adapter.begin_action(lease, ActionRequest("task-1", "action-1", "navigate"))

    with pytest.raises(ValueError, match="byte bound"):
        await adapter.finish_action(
            capture,
            ActionResult(OutcomeStatus.SUCCEEDED, "done"),
            observation("o1"),
        )

    pointer = tmp_path / "state" / "actions" / lease.episode_id / f"{capture.capture_id}.json"
    payload = json.loads(pointer.read_text())
    assert payload["complete"] is False
    assert "required_artifact_too_large" in payload["errors"]
