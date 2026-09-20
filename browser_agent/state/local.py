"""Local filesystem browser-state adapter."""

from __future__ import annotations

import json
import os
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..browser.models import ActionResult, Observation
from .artifacts import ArtifactStore
from .checkpoints import (
    archive_profile,
    artifact_to_dict,
    decode_manifest,
    encode_manifest,
    extract_profile,
    metadata_from_manifest,
    policy_from_manifest,
    profile_reference,
)
from .models import (
    ActionCapture,
    ActionRequest,
    ArtifactKind,
    ArtifactRef,
    CapturePolicy,
    CheckpointError,
    CheckpointRef,
    DiagnosticRef,
    EpisodeLease,
    EpisodeMetadata,
    EpisodeOutcome,
    LeaseClosedError,
    SecurityClass,
    StateDelta,
)


@dataclass(slots=True)
class _Episode:
    lease: EpisodeLease
    directory: Path
    profile: Path
    policy: CapturePolicy
    metadata: EpisodeMetadata


class LocalBrowserStateAdapter:
    """Own unique writable profiles and publish stopped profiles as checkpoints."""

    def __init__(self, root: Path, artifacts: ArtifactStore) -> None:
        self._root = root
        self._artifacts = artifacts
        self._episodes = root / "episodes"
        self._quarantine = root / "quarantine"
        self._baseline = root / "baseline"
        self._episodes.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._baseline.exists():
            self._baseline.mkdir(parents=True, mode=0o500)
        self._open: dict[str, _Episode] = {}
        self._captures: dict[str, ActionCapture] = {}

    async def open_episode(
        self,
        seed_checkpoint: CheckpointRef | None,
        policy: CapturePolicy,
        metadata: EpisodeMetadata,
    ) -> EpisodeLease:
        source = self._baseline
        temporary_source: Path | None = None
        if seed_checkpoint is not None:
            temporary_source = await self._materialize_checkpoint(seed_checkpoint)
            source = temporary_source
        try:
            return self._clone_episode(source, seed_checkpoint, policy, metadata)
        finally:
            if temporary_source is not None:
                shutil.rmtree(temporary_source, ignore_errors=True)

    async def begin_action(self, lease: EpisodeLease, request: ActionRequest) -> ActionCapture:
        self._require_open(lease)
        capture = ActionCapture(uuid4().hex, lease.episode_id, request)
        self._captures[capture.capture_id] = capture
        return capture

    async def finish_action(
        self,
        capture: ActionCapture,
        result: ActionResult,
        observation: Observation | None,
    ) -> StateDelta:
        current = self._captures.pop(capture.capture_id, None)
        if current != capture or not self._episode_is_open(capture.episode_id):
            raise LeaseClosedError("action capture is not open")
        return StateDelta(
            delta_id=uuid4().hex,
            episode_id=capture.episode_id,
            task_id=capture.request.task_id,
            action_id=capture.request.action_id,
            pre_observation_id=capture.request.pre_observation_id,
            post_observation_id=observation.observation_id if observation else None,
            outcome=result.status,
        )

    async def checkpoint(self, lease: EpisodeLease, reason: str) -> CheckpointRef:
        return await self._seal(lease, reason)

    async def close_episode(self, lease: EpisodeLease, outcome: EpisodeOutcome) -> CheckpointRef:
        return await self._seal(lease, f"terminal:{outcome}")

    async def abort_episode(self, lease: EpisodeLease, error: BaseException) -> DiagnosticRef:
        episode = self._take(lease)
        self._discard_captures(lease.episode_id)
        try:
            diagnostic = await self._diagnostic(
                episode,
                phase="abort",
                error_type=type(error).__name__,
            )
        finally:
            shutil.rmtree(episode.directory, ignore_errors=True)
        return diagnostic

    async def restore(self, checkpoint: CheckpointRef) -> EpisodeLease:
        manifest = await self._load_manifest(checkpoint)
        policy = policy_from_manifest(manifest)
        metadata = metadata_from_manifest(manifest)
        return await self.open_episode(checkpoint, policy, metadata)

    async def branch(self, checkpoint: CheckpointRef, count: int) -> list[EpisodeLease]:
        if count < 1:
            raise ValueError("branch count must be greater than zero")
        return [await self.restore(checkpoint) for _ in range(count)]

    def _profile_directory(self, lease: EpisodeLease) -> Path:
        """Internal browser/state boundary: resolve profile for browser launch."""
        return self._require_open(lease).profile

    def _clone_episode(
        self,
        source: Path,
        seed: CheckpointRef | None,
        policy: CapturePolicy,
        metadata: EpisodeMetadata,
    ) -> EpisodeLease:
        episode_id = uuid4().hex
        lease = EpisodeLease(uuid4().hex, episode_id, seed.checkpoint_id if seed else None)
        directory = self._episodes / episode_id
        profile = directory / "profile"
        directory.mkdir(mode=0o700)
        shutil.copytree(source, profile)
        self._make_writable(profile)
        self._open[lease.lease_id] = _Episode(lease, directory, profile, policy, metadata)
        return lease

    async def _seal(self, lease: EpisodeLease, reason: str) -> CheckpointRef:
        episode = self._take(lease)
        self._discard_captures(lease.episode_id)
        profile_ref: ArtifactRef | None = None
        try:
            if not episode.policy.restricted_storage:
                raise CheckpointError("capture policy forbids restricted checkpoint storage")
            self._verify_stopped_profile(episode.profile)
            profile_bytes, files = archive_profile(episode.profile)
            profile_ref = await self._artifacts.put(
                profile_bytes,
                kind=ArtifactKind.CHECKPOINT,
                media_type="application/x-tar",
                schema_version=1,
                security_class=SecurityClass.RESTRICTED,
                redaction_policy_version=episode.policy.redaction_policy_version,
            )
            manifest_bytes = encode_manifest(
                episode_id=episode.lease.episode_id,
                parent_id=episode.lease.parent_checkpoint_id,
                reason=reason,
                profile=profile_ref,
                files=files,
                policy=episode.policy,
                metadata=episode.metadata,
            )
            manifest_ref = await self._artifacts.put(
                manifest_bytes,
                kind=ArtifactKind.CHECKPOINT,
                media_type="application/json",
                schema_version=1,
                security_class=SecurityClass.RESTRICTED,
                redaction_policy_version=episode.policy.redaction_policy_version,
            )
        except BaseException as error:
            try:
                await self._diagnostic(
                    episode,
                    phase="seal",
                    error_type=type(error).__name__,
                    quarantined_profile=profile_ref,
                )
            finally:
                shutil.rmtree(episode.directory, ignore_errors=True)
            raise CheckpointError("checkpoint sealing failed") from error

        shutil.rmtree(episode.directory)
        return CheckpointRef(
            checkpoint_id=manifest_ref.content_id,
            episode_id=lease.episode_id,
            parent_id=lease.parent_checkpoint_id,
            reason=reason,
            clean_shutdown=True,
            manifest=manifest_ref,
        )

    async def _diagnostic(
        self,
        episode: _Episode,
        *,
        phase: str,
        error_type: str,
        quarantined_profile: ArtifactRef | None = None,
    ) -> DiagnosticRef:
        data = json.dumps(
            {
                "episode_id": episode.lease.episode_id,
                "error_type": error_type,
                "phase": phase,
                "profile_retained": quarantined_profile is not None,
                "quarantined_profile": (
                    artifact_to_dict(quarantined_profile) if quarantined_profile else None
                ),
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        artifact: ArtifactRef | None = None
        with suppress(Exception):
            artifact = await self._artifacts.put(
                data,
                kind=ArtifactKind.DIAGNOSTIC,
                media_type="application/json",
                schema_version=1,
                security_class=SecurityClass.REDACTED,
                redaction_policy_version=episode.policy.redaction_policy_version,
            )
        diagnostic_id = artifact.content_id if artifact else f"diagnostic:{uuid4().hex}"
        with suppress(Exception):
            self._publish_quarantine_pointer(diagnostic_id, data, artifact)
        return DiagnosticRef(diagnostic_id, episode.lease.episode_id, artifact)

    def _publish_quarantine_pointer(
        self, diagnostic_id: str, diagnostic: bytes, artifact: ArtifactRef | None
    ) -> None:
        pointer = json.dumps(
            {
                "artifact_id": artifact.content_id if artifact else None,
                "diagnostic_id": diagnostic_id,
                "diagnostic": json.loads(diagnostic),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        destination = self._quarantine / f"{uuid4().hex}.json"
        temporary = self._quarantine / f".{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(pointer)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    async def _materialize_checkpoint(self, checkpoint: CheckpointRef) -> Path:
        manifest = await self._load_manifest(checkpoint)
        profile = profile_reference(manifest)
        archive = await self._artifacts.get(profile)
        destination = self._root / f"restore-{uuid4().hex}"
        destination.mkdir(mode=0o700)
        try:
            extract_profile(archive, destination)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return destination

    async def _load_manifest(self, checkpoint: CheckpointRef) -> dict[str, object]:
        if checkpoint.manifest is None:
            raise CheckpointError("checkpoint has no manifest")
        try:
            return decode_manifest(await self._artifacts.get(checkpoint.manifest))
        except Exception as error:
            raise CheckpointError("checkpoint manifest cannot be read") from error

    def _require_open(self, lease: EpisodeLease) -> _Episode:
        episode = self._open.get(lease.lease_id)
        if episode is None or episode.lease != lease:
            raise LeaseClosedError("episode lease is not open")
        return episode

    def _take(self, lease: EpisodeLease) -> _Episode:
        episode = self._require_open(lease)
        del self._open[lease.lease_id]
        return episode

    def _episode_is_open(self, episode_id: str) -> bool:
        return any(item.lease.episode_id == episode_id for item in self._open.values())

    def _discard_captures(self, episode_id: str) -> None:
        self._captures = {
            key: value for key, value in self._captures.items() if value.episode_id != episode_id
        }

    @staticmethod
    def _make_writable(root: Path) -> None:
        root.chmod(0o700)
        for path in root.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)

    @staticmethod
    def _verify_stopped_profile(profile: Path) -> None:
        active_markers = ("SingletonCookie", "SingletonLock", "SingletonSocket")
        if any(
            (profile / name).exists() or (profile / name).is_symlink() for name in active_markers
        ):
            raise CheckpointError("browser profile is not verified stopped")
