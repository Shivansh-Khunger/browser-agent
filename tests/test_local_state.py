from __future__ import annotations

import json
from dataclasses import replace

import pytest

from browser_agent.state.artifacts import LocalArtifactStore
from browser_agent.state.local import LocalBrowserStateAdapter
from browser_agent.state.models import (
    ArtifactKind,
    ArtifactRef,
    CapturePolicy,
    CheckpointError,
    EpisodeMetadata,
    EpisodeOutcome,
    LeaseClosedError,
    RestrictedStorageError,
    SecurityClass,
)
from tests.fakes import clean_shutdown


class _FailingManifestStore:
    def __init__(self, backing: LocalArtifactStore) -> None:
        self._backing = backing

    async def put(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        media_type: str,
        schema_version: int,
        security_class: SecurityClass,
        redaction_policy_version: str,
    ) -> ArtifactRef:
        if kind is ArtifactKind.CHECKPOINT and media_type == "application/json":
            raise OSError("injected manifest publication failure")
        return await self._backing.put(
            data,
            kind=kind,
            media_type=media_type,
            schema_version=schema_version,
            security_class=security_class,
            redaction_policy_version=redaction_policy_version,
        )

    async def get(self, reference: ArtifactRef) -> bytes:
        return await self._backing.get(reference)


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

    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
    checkpoint = await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)
    assert checkpoint.clean_shutdown is True
    assert checkpoint.parent_id is None
    assert checkpoint.manifest is not None
    assert checkpoint.manifest.security_class is SecurityClass.RESTRICTED
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

    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
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

    await adapter.confirm_shutdown(first, clean_shutdown(first.episode_id))
    checkpoint = await adapter.close_episode(first, EpisodeOutcome.SUCCEEDED)
    assert checkpoint.manifest is not None
    original_manifest = await store.get(checkpoint.manifest)
    restored = await adapter.restore(checkpoint)
    assert (
        adapter._profile_directory(restored) / "Cookies"
    ).read_bytes() == b"first-profile-secret"
    (adapter._profile_directory(restored) / "Cookies").write_bytes(b"child-secret")
    await adapter.confirm_shutdown(restored, clean_shutdown(restored.episode_id))
    await adapter.close_episode(restored, EpisodeOutcome.SUCCEEDED)

    assert await store.get(checkpoint.manifest) == original_manifest
    await adapter.abort_episode(second, RuntimeError("test cleanup"))
    assert all(
        b"first-profile-secret" not in path.read_bytes()
        and b"child-secret" not in path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    )


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

    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
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


@pytest.mark.asyncio
async def test_active_profile_marker_prevents_checkpoint_publication(tmp_path) -> None:
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
    (adapter._profile_directory(lease) / "SingletonLock").write_text("still running")

    with pytest.raises(CheckpointError):
        await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
    diagnostic = await adapter.abort_episode(lease, RuntimeError("browser still running"))

    assert diagnostic.artifact is not None
    pointers = list((tmp_path / "state" / "quarantine").glob("*.json"))
    data = json.loads(pointers[0].read_text())["diagnostic"]
    assert data["profile_retained"] is True


@pytest.mark.asyncio
async def test_checkpoint_requires_process_exit_proof(tmp_path) -> None:
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
    secret = b"unverified-shutdown-canary"
    (adapter._profile_directory(lease) / "Cookies").write_bytes(secret)

    with pytest.raises(CheckpointError):
        await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)

    pointers = list((tmp_path / "state" / "quarantine").glob("*.json"))
    diagnostic = json.loads(pointers[0].read_text())["diagnostic"]
    assert diagnostic["profile_retained"] is True
    assert all(secret not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.asyncio
async def test_manifest_publication_failure_keeps_only_encrypted_quarantine(tmp_path) -> None:
    backing = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", _FailingManifestStore(backing))
    lease = await adapter.open_episode(
        None,
        CapturePolicy(
            version="capture-v1",
            redaction_policy_version="redaction-v1",
            restricted_storage=True,
        ),
        EpisodeMetadata(code_revision="abc123", platform="test"),
    )
    secret = b"atomic-publication-canary"
    (adapter._profile_directory(lease) / "Cookies").write_bytes(secret)

    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
    with pytest.raises(CheckpointError):
        await adapter.close_episode(lease, EpisodeOutcome.SUCCEEDED)

    pointers = list((tmp_path / "state" / "quarantine").glob("*.json"))
    assert len(pointers) == 1
    diagnostic = json.loads(pointers[0].read_text())["diagnostic"]
    assert diagnostic["profile_retained"] is True
    assert diagnostic["quarantined_profile"]["content_id"].startswith("hmac-sha256:")
    assert all(secret not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.asyncio
async def test_restore_validates_lineage_clean_shutdown_and_browser_compatibility(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    policy = CapturePolicy("capture-v1", "redaction-v1", restricted_storage=True)
    writer = EpisodeMetadata(
        code_revision="writer",
        platform="Linux-writer",
        browser_version="Chrome 140.0.0.0",
        nodriver_version="0.50.3",
    )
    lease = await adapter.open_episode(None, policy, writer)
    (adapter._profile_directory(lease) / "Cookies").write_bytes(b"restore-fidelity")
    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
    checkpoint = await adapter.checkpoint(lease, "explicit")

    current = replace(writer, code_revision="reader", browser_version="Chrome 141.0.0.0")
    restored = await adapter.open_episode(checkpoint, policy, current)
    assert (adapter._profile_directory(restored) / "Cookies").read_bytes() == b"restore-fidelity"
    await adapter.abort_episode(restored, RuntimeError("cleanup"))

    with pytest.raises(CheckpointError, match="downgrade"):
        await adapter.open_episode(
            checkpoint, policy, replace(current, browser_version="Chrome 139.0.0.0")
        )
    with pytest.raises(CheckpointError, match="lineage"):
        await adapter.open_episode(replace(checkpoint, reason="forged"), policy, current)
    with pytest.raises(CheckpointError, match="clean shutdown"):
        await adapter.open_episode(replace(checkpoint, clean_shutdown=False), policy, current)
    with pytest.raises(CheckpointError, match="schema version"):
        await adapter.open_episode(replace(checkpoint, schema_version=2), policy, current)
    with pytest.raises(CheckpointError, match="platform"):
        await adapter.open_episode(checkpoint, policy, replace(current, platform="Darwin-reader"))


@pytest.mark.asyncio
async def test_restore_warns_or_rejects_nodriver_version_transition(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    writer_policy = CapturePolicy("capture-v1", "redaction-v1", restricted_storage=True)
    writer = EpisodeMetadata(
        code_revision="writer",
        platform="Linux-writer",
        browser_version="Chrome 140.0.0.0",
        nodriver_version="0.50.3",
    )
    lease = await adapter.open_episode(None, writer_policy, writer)
    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
    checkpoint = await adapter.checkpoint(lease, "explicit")
    current = replace(writer, nodriver_version="0.51.0")

    restored = await adapter.open_episode(checkpoint, writer_policy, current)
    assert restored.compatibility_warnings == ("nodriver_version_changed:0.50.3->0.51.0",)
    await adapter.abort_episode(restored, RuntimeError("cleanup"))

    rejecting_policy = replace(
        writer_policy,
        incompatible_nodriver_transitions=frozenset({("0.50.3", "0.51.0")}),
    )
    with pytest.raises(CheckpointError, match="configured incompatible"):
        await adapter.open_episode(checkpoint, rejecting_policy, current)


@pytest.mark.asyncio
async def test_branch_profiles_are_independent_and_source_remains_immutable(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    policy = CapturePolicy("capture-v1", "redaction-v1", restricted_storage=True)
    metadata = EpisodeMetadata(code_revision="writer", platform="test")
    lease = await adapter.open_episode(None, policy, metadata)
    (adapter._profile_directory(lease) / "Cookies").write_bytes(b"parent")
    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
    checkpoint = await adapter.checkpoint(lease, "branch-source")

    first, second = await adapter.branch(checkpoint, 2)
    (adapter._profile_directory(first) / "Cookies").write_bytes(b"first")

    assert (adapter._profile_directory(second) / "Cookies").read_bytes() == b"parent"
    third = await adapter.restore(checkpoint)
    assert (adapter._profile_directory(third) / "Cookies").read_bytes() == b"parent"
    assert len({first.episode_id, second.episode_id, third.episode_id}) == 3
    for child in (first, second, third):
        await adapter.abort_episode(child, RuntimeError("cleanup"))


@pytest.mark.asyncio
async def test_restore_rejects_profile_that_disagrees_with_file_manifest(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts", encryption_key=b"k" * 32)
    adapter = LocalBrowserStateAdapter(tmp_path / "state", store)
    policy = CapturePolicy("capture-v1", "redaction-v1", restricted_storage=True)
    metadata = EpisodeMetadata(code_revision="writer", platform="test")
    lease = await adapter.open_episode(None, policy, metadata)
    (adapter._profile_directory(lease) / "Cookies").write_bytes(b"profile")
    await adapter.confirm_shutdown(lease, clean_shutdown(lease.episode_id))
    checkpoint = await adapter.checkpoint(lease, "explicit")
    assert checkpoint.manifest is not None
    manifest = json.loads((await store.get(checkpoint.manifest)).decode())
    manifest["files"][0]["sha256"] = "0" * 64
    forged_manifest = await store.put(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        kind=ArtifactKind.CHECKPOINT,
        media_type="application/json",
        schema_version=1,
        security_class=SecurityClass.RESTRICTED,
        redaction_policy_version="redaction-v1",
    )
    forged = replace(
        checkpoint,
        checkpoint_id=forged_manifest.content_id,
        manifest=forged_manifest,
    )

    with pytest.raises(CheckpointError, match="file manifest"):
        await adapter.restore(forged)
