from __future__ import annotations

import json

import pytest

from browser_agent.state.artifacts import LocalArtifactStore
from browser_agent.state.local import LocalBrowserStateAdapter
from browser_agent.state.models import (
    ArtifactKind,
    CapturePolicy,
    CheckpointError,
    EpisodeMetadata,
    EpisodeOutcome,
    LeaseClosedError,
    RestrictedStorageError,
    SecurityClass,
)


@pytest.mark.asyncio
async def test_restricted_artifact_is_encrypted_and_content_addressed(tmp_path) -> None:
    secret = b"session-cookie=secret-canary"
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)

    first = await store.put(
        secret,
        kind=ArtifactKind.CHECKPOINT,
        media_type="application/x-tar",
        schema_version=1,
        security_class=SecurityClass.RESTRICTED,
        redaction_policy_version="redaction-v1",
    )
    second = await store.put(
        secret,
        kind=ArtifactKind.CHECKPOINT,
        media_type="application/x-tar",
        schema_version=1,
        security_class=SecurityClass.RESTRICTED,
        redaction_policy_version="redaction-v1",
    )

    assert first == second
    assert first.content_id.startswith("hmac-sha256:")
    assert await store.get(first) == secret
    assert all(secret not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert not any((tmp_path / "artifacts" / ".tmp").iterdir())


@pytest.mark.asyncio
async def test_restricted_artifact_without_key_is_not_persisted(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    with pytest.raises(RestrictedStorageError):
        await store.put(
            b"reusable-credential",
            kind=ArtifactKind.CHECKPOINT,
            media_type="application/x-tar",
            schema_version=1,
            security_class=SecurityClass.RESTRICTED,
            redaction_policy_version="redaction-v1",
        )

    assert not any(path.is_file() for path in tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_restricted_dedup_remains_readable_across_reference_metadata(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    first = await store.put(
        b"same restricted bytes",
        kind=ArtifactKind.CHECKPOINT,
        media_type="application/x-tar",
        schema_version=1,
        security_class=SecurityClass.RESTRICTED,
        redaction_policy_version="redaction-v1",
    )
    second = await store.put(
        b"same restricted bytes",
        kind=ArtifactKind.DIAGNOSTIC,
        media_type="application/octet-stream",
        schema_version=2,
        security_class=SecurityClass.RESTRICTED,
        redaction_policy_version="redaction-v2",
    )

    assert first.content_id == second.content_id
    assert await store.get(first) == b"same restricted bytes"
    assert await store.get(second) == b"same restricted bytes"


@pytest.mark.asyncio
async def test_fresh_episode_seals_encrypted_checkpoint_and_restores_child(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    lease = await adapter.open_episode(
        None,
        CapturePolicy(
            version="capture-v1",
            redaction_policy_version="redaction-v1",
            restricted_storage=True,
        ),
        EpisodeMetadata(code_revision="abc123", platform="test"),
    )

    checkpoint = await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)
    assert checkpoint.clean_shutdown is True
    assert checkpoint.parent_id is None
    assert checkpoint.manifest is not None
    assert not hasattr(lease, "profile_path")

    manifest = json.loads((await store.get(checkpoint.manifest)).decode())
    assert manifest["clean_shutdown"] is True
    assert manifest["profile"]["security_class"] == "restricted"

    restored = await adapter.restore(checkpoint)
    assert restored.parent_checkpoint_id == checkpoint.checkpoint_id
    assert restored.episode_id != lease.episode_id


@pytest.mark.asyncio
async def test_episode_without_restricted_storage_permission_cannot_seal(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    lease = await adapter.open_episode(
        None,
        CapturePolicy(version="capture-v1", redaction_policy_version="redaction-v1"),
        EpisodeMetadata(code_revision="abc123", platform="test"),
    )

    with pytest.raises(CheckpointError):
        await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)

    assert not list((tmp_path / "artifacts" / "restricted").rglob("*"))


@pytest.mark.asyncio
async def test_episode_profiles_are_isolated_and_sealed_source_stays_immutable(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    policy = CapturePolicy(
        version="capture-v1",
        redaction_policy_version="redaction-v1",
        restricted_storage=True,
    )
    metadata = EpisodeMetadata(code_revision="abc123", platform="test")
    first = await adapter.open_episode(None, policy, metadata)
    second = await adapter.open_episode(None, policy, metadata)

    (adapter._profile_directory(first) / "Cookies").write_bytes(b"first-profile-secret")
    assert not (adapter._profile_directory(second) / "Cookies").exists()

    checkpoint = await adapter.close_episode(first, EpisodeOutcome.SUCCEEDED)
    assert checkpoint.manifest is not None
    original_manifest = await store.get(checkpoint.manifest)
    restored = await adapter.restore(checkpoint)
    assert (
        adapter._profile_directory(restored) / "Cookies"
    ).read_bytes() == b"first-profile-secret"
    (adapter._profile_directory(restored) / "Cookies").write_bytes(b"child-secret")
    await adapter.close_episode(restored, EpisodeOutcome.SUCCEEDED)

    assert await store.get(checkpoint.manifest) == original_manifest
    await adapter.abort_episode(second, RuntimeError("test cleanup"))


@pytest.mark.asyncio
async def test_missing_key_seal_discards_profile_and_invalidates_lease(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    lease = await adapter.open_episode(
        None,
        CapturePolicy(
            version="capture-v1",
            redaction_policy_version="redaction-v1",
            restricted_storage=True,
        ),
        EpisodeMetadata(code_revision="abc123", platform="test"),
    )
    secret = b"unsealed-profile-canary"
    (adapter._profile_directory(lease) / "Cookies").write_bytes(secret)

    with pytest.raises(CheckpointError):
        await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)
    with pytest.raises(LeaseClosedError):
        await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)

    assert all(secret not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.asyncio
async def test_abort_returns_redacted_diagnostic_and_discards_profile(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    lease = await adapter.open_episode(
        None,
        CapturePolicy(version="capture-v1", redaction_policy_version="redaction-v1"),
        EpisodeMetadata(code_revision="abc123", platform="test"),
    )

    diagnostic = await adapter.abort_episode(lease, RuntimeError("secret error text"))

    assert diagnostic.artifact is not None
    data = await store.get(diagnostic.artifact)
    assert b"RuntimeError" in data
    assert b"secret error text" not in data
    with pytest.raises(LeaseClosedError):
        await adapter.abort_episode(lease, RuntimeError("again"))
