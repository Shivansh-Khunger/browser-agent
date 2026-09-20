"""Local filesystem browser-state adapter."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast
from uuid import uuid4

from ..browser.models import ActionResult, Observation
from .artifacts import ArtifactStore
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
        policy = self._policy_from_manifest(manifest)
        metadata = self._metadata_from_manifest(manifest)
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
        try:
            if not episode.policy.restricted_storage:
                raise CheckpointError("capture policy forbids restricted checkpoint storage")
            files = self._file_manifest(episode.profile)
            profile_bytes = self._archive(episode.profile)
            profile_ref = await self._artifacts.put(
                profile_bytes,
                kind=ArtifactKind.CHECKPOINT,
                media_type="application/x-tar",
                schema_version=1,
                security_class=SecurityClass.RESTRICTED,
                redaction_policy_version=episode.policy.redaction_policy_version,
            )
            manifest_bytes = self._manifest_bytes(episode, reason, profile_ref, files)
            manifest_ref = await self._artifacts.put(
                manifest_bytes,
                kind=ArtifactKind.CHECKPOINT,
                media_type="application/json",
                schema_version=1,
                security_class=SecurityClass.REDACTED,
                redaction_policy_version=episode.policy.redaction_policy_version,
            )
        except BaseException as error:
            try:
                await self._diagnostic(episode, phase="seal", error_type=type(error).__name__)
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

    async def _diagnostic(self, episode: _Episode, *, phase: str, error_type: str) -> DiagnosticRef:
        data = json.dumps(
            {
                "episode_id": episode.lease.episode_id,
                "error_type": error_type,
                "phase": phase,
                "profile_retained": False,
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

    def _manifest_bytes(
        self,
        episode: _Episode,
        reason: str,
        profile: ArtifactRef,
        files: list[dict[str, object]],
    ) -> bytes:
        manifest = {
            "schema_version": 1,
            "episode_id": episode.lease.episode_id,
            "parent_id": episode.lease.parent_checkpoint_id,
            "reason": reason,
            "clean_shutdown": True,
            "created_at": datetime.now(UTC).isoformat(),
            "profile": self._artifact_dict(profile),
            "files": files,
            "policy": {
                "version": episode.policy.version,
                "redaction_policy_version": episode.policy.redaction_policy_version,
                "max_queue_items": episode.policy.max_queue_items,
                "max_artifact_bytes": episode.policy.max_artifact_bytes,
                "capture_timeout": episode.policy.capture_timeout,
                "restricted_storage": episode.policy.restricted_storage,
                "optional_artifacts": sorted(
                    item.value for item in episode.policy.optional_artifacts
                ),
                "retention_labels": list(episode.policy.retention_labels),
            },
            "metadata": asdict(episode.metadata),
        }
        return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()

    async def _materialize_checkpoint(self, checkpoint: CheckpointRef) -> Path:
        manifest = await self._load_manifest(checkpoint)
        profile = self._artifact_from_dict(manifest.get("profile"))
        archive = await self._artifacts.get(profile)
        destination = self._root / f"restore-{uuid4().hex}"
        destination.mkdir(mode=0o700)
        try:
            self._extract(archive, destination)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        return destination

    async def _load_manifest(self, checkpoint: CheckpointRef) -> dict[str, object]:
        if checkpoint.manifest is None:
            raise CheckpointError("checkpoint has no manifest")
        try:
            raw: object = json.loads((await self._artifacts.get(checkpoint.manifest)).decode())
        except Exception as error:
            raise CheckpointError("checkpoint manifest cannot be read") from error
        return self._object_dict(raw, "checkpoint manifest is not an object")

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
    def _archive(root: Path) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise CheckpointError("profile symlinks are not supported")
                relative = path.relative_to(root).as_posix()
                info = archive.gettarinfo(str(path), arcname=relative)
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                if path.is_file():
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                elif path.is_dir():
                    archive.addfile(info)
        return buffer.getvalue()

    @staticmethod
    def _extract(data: bytes, destination: Path) -> None:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise CheckpointError("checkpoint contains an unsafe path")
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    source = archive.extractfile(member)
                    if source is None:
                        raise CheckpointError("checkpoint file cannot be read")
                    with target.open("wb") as stream:
                        shutil.copyfileobj(source, stream)
                    target.chmod(0o600)
                else:
                    raise CheckpointError("checkpoint contains an unsupported entry")

    @staticmethod
    def _file_manifest(root: Path) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                files.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
        return files

    @staticmethod
    def _artifact_dict(reference: ArtifactRef) -> dict[str, object]:
        return {
            "content_id": reference.content_id,
            "kind": reference.kind.value,
            "media_type": reference.media_type,
            "schema_version": reference.schema_version,
            "encoding": reference.encoding,
            "compression": reference.compression,
            "byte_length": reference.byte_length,
            "security_class": reference.security_class.value,
            "redaction_policy_version": reference.redaction_policy_version,
            "restricted_locator": reference.restricted_locator,
        }

    @staticmethod
    def _artifact_from_dict(value: object) -> ArtifactRef:
        value = LocalBrowserStateAdapter._object_dict(
            value, "checkpoint profile reference is invalid"
        )
        try:
            return ArtifactRef(
                content_id=str(value["content_id"]),
                kind=ArtifactKind(str(value["kind"])),
                media_type=str(value["media_type"]),
                schema_version=LocalBrowserStateAdapter._integer(value["schema_version"]),
                encoding=str(value["encoding"]),
                compression=str(value["compression"]) if value["compression"] else None,
                byte_length=LocalBrowserStateAdapter._integer(value["byte_length"]),
                security_class=SecurityClass(str(value["security_class"])),
                redaction_policy_version=str(value["redaction_policy_version"]),
                restricted_locator=(
                    str(value["restricted_locator"]) if value["restricted_locator"] else None
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CheckpointError("checkpoint profile reference is invalid") from error

    @staticmethod
    def _policy_from_manifest(manifest: dict[str, object]) -> CapturePolicy:
        value = LocalBrowserStateAdapter._object_dict(
            manifest.get("policy"), "checkpoint policy is invalid"
        )
        try:
            return CapturePolicy(
                version=str(value["version"]),
                redaction_policy_version=str(value["redaction_policy_version"]),
                optional_artifacts=frozenset(
                    ArtifactKind(item)
                    for item in LocalBrowserStateAdapter._string_list(
                        value.get("optional_artifacts"), "checkpoint policy is invalid"
                    )
                ),
                max_queue_items=LocalBrowserStateAdapter._integer(value["max_queue_items"]),
                max_artifact_bytes=LocalBrowserStateAdapter._integer(value["max_artifact_bytes"]),
                capture_timeout=LocalBrowserStateAdapter._number(value["capture_timeout"]),
                restricted_storage=LocalBrowserStateAdapter._boolean(value["restricted_storage"]),
                retention_labels=tuple(
                    LocalBrowserStateAdapter._string_list(
                        value.get("retention_labels"), "checkpoint policy is invalid"
                    )
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CheckpointError("checkpoint policy is invalid") from error

    @staticmethod
    def _metadata_from_manifest(manifest: dict[str, object]) -> EpisodeMetadata:
        value = LocalBrowserStateAdapter._object_dict(
            manifest.get("metadata"), "checkpoint metadata is invalid"
        )
        try:
            return EpisodeMetadata(
                code_revision=str(value["code_revision"]),
                platform=str(value["platform"]),
                task_id=LocalBrowserStateAdapter._optional_string(value.get("task_id")),
                browser_executable=LocalBrowserStateAdapter._optional_string(
                    value.get("browser_executable")
                ),
                browser_version=LocalBrowserStateAdapter._optional_string(
                    value.get("browser_version")
                ),
                nodriver_version=LocalBrowserStateAdapter._optional_string(
                    value.get("nodriver_version")
                ),
                config_digest=LocalBrowserStateAdapter._optional_string(value.get("config_digest")),
                locale_override=LocalBrowserStateAdapter._optional_string(
                    value.get("locale_override")
                ),
                timezone_override=LocalBrowserStateAdapter._optional_string(
                    value.get("timezone_override")
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CheckpointError("checkpoint metadata is invalid") from error

    @staticmethod
    def _object_dict(value: object, message: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise CheckpointError(message)
        untyped = cast(dict[object, object], value)
        if not all(isinstance(key, str) for key in untyped):
            raise CheckpointError(message)
        return cast(dict[str, object], value)

    @staticmethod
    def _string_list(value: object, message: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise CheckpointError(message)
        untyped = cast(list[object], value)
        if not all(isinstance(item, str) for item in untyped):
            raise CheckpointError(message)
        return cast(list[str], value)

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise CheckpointError("checkpoint metadata is invalid")
        return value

    @staticmethod
    def _integer(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CheckpointError("checkpoint numeric value is invalid")
        return value

    @staticmethod
    def _number(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise CheckpointError("checkpoint numeric value is invalid")
        return float(value)

    @staticmethod
    def _boolean(value: object) -> bool:
        if not isinstance(value, bool):
            raise CheckpointError("checkpoint boolean value is invalid")
        return value
