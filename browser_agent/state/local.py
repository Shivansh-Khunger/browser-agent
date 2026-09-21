"""Local filesystem browser-state adapter."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..browser.models import ActionResult, BrowserAction, Observation
from .artifacts import ArtifactStore
from .capture import (
    canonical_json,
    observation_digest,
    observation_payload,
    redact_action_input,
    redact_network_event,
    redact_result,
    redact_storage_event,
    redact_url,
    utc_now,
)
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
    ActionEvidence,
    ActionRequest,
    ArtifactKind,
    ArtifactRef,
    CapturePolicy,
    CheckpointError,
    CheckpointRef,
    CleanShutdownProof,
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
    shutdown: CleanShutdownProof | None = None


class LocalBrowserStateAdapter:
    """Own unique writable profiles and publish stopped profiles as checkpoints."""

    def __init__(self, root: Path, artifacts: ArtifactStore) -> None:
        self._root = root
        self._artifacts = artifacts
        self._episodes = root / "episodes"
        self._quarantine = root / "quarantine"
        self._action_records = root / "actions"
        self._network_streams = root / "network"
        self._baseline = root / "baseline"
        self._episodes.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._quarantine.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._action_records.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._network_streams.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self._baseline.exists():
            self._baseline.mkdir(parents=True, mode=0o500)
        self._open: dict[str, _Episode] = {}
        self._captures: dict[str, ActionCapture] = {}
        self._capture_condition = asyncio.Condition()

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
        episode = self._require_open(lease)
        async with asyncio.timeout(episode.policy.capture_timeout):
            async with self._capture_condition:
                await self._capture_condition.wait_for(
                    lambda: (
                        sum(item.episode_id == lease.episode_id for item in self._captures.values())
                        < episode.policy.max_queue_items
                    )
                )
        capture = ActionCapture(
            uuid4().hex,
            lease.episode_id,
            request,
            started_at=utc_now(),
            started_monotonic=time.monotonic(),
        )
        self._captures[capture.capture_id] = capture
        safe_input = self._redacted_request_input(request)
        self._publish_action_pointer(
            capture,
            {
                "schema_version": 1,
                "capture_id": capture.capture_id,
                "episode_id": capture.episode_id,
                "task_id": request.task_id,
                "action_id": request.action_id,
                "action": request.name,
                "input": safe_input,
                "target": str(request.target) if request.target else None,
                "pre_observation_id": request.pre_observation_id,
                "started_at": capture.started_at,
                "complete": False,
            },
        )
        return capture

    async def finish_action(
        self,
        capture: ActionCapture,
        result: ActionResult,
        observation: Observation | None,
        evidence: ActionEvidence | None = None,
    ) -> StateDelta:
        current = self._captures.get(capture.capture_id)
        if current != capture or not self._episode_is_open(capture.episode_id):
            raise LeaseClosedError("action capture is not open")
        episode = self._episode_for_id(capture.episode_id)
        browser = evidence.browser if evidence is not None else None
        all_network_events = (
            tuple(redact_network_event(event) for event in browser.network_events)
            if browser
            else ()
        )
        all_storage_events = (
            tuple(redact_storage_event(event) for event in browser.storage_events)
            if browser
            else ()
        )
        dropped = max(0, len(all_network_events) - episode.policy.max_queue_items) + max(
            0, len(all_storage_events) - episode.policy.max_queue_items
        )
        network_events = all_network_events[-episode.policy.max_queue_items :]
        storage_events = all_storage_events[-episode.policy.max_queue_items :]
        network_span = (
            (network_events[0].sequence, network_events[-1].sequence) if network_events else None
        )
        finished_at = utc_now()
        duration_ms = max(
            0.0,
            (time.monotonic() - capture.started_monotonic) * 1000
            if capture.started_monotonic is not None
            else 0.0,
        )
        post_digest = observation_digest(observation)
        pre_url = redact_url(capture.request.pre_url) if capture.request.pre_url else None
        post_url = redact_url(observation.url) if observation else None
        warnings = tuple(browser.warnings if browser else ()) + tuple(
            observation.warnings if observation else ()
        )
        if dropped:
            warnings = (*warnings, "capacity_dropped")
        errors = (
            (result.error_code or "action_failed",)
            if result.status.value in {"failed", "uncertain"}
            else ()
        )
        if browser and browser.required_failure:
            errors = (*errors, browser.required_failure)
        omissions = tuple(browser.omissions if browser else ())
        if dropped:
            omissions = (*omissions, f"capacity_dropped:{dropped}")
        (
            optional_artifacts,
            optional_warnings,
            optional_omissions,
        ) = await self._capture_optional_artifacts(episode, observation, network_events)
        warnings = (*warnings, *optional_warnings)
        omissions = (*omissions, *optional_omissions)
        delta_id = uuid4().hex
        payload = {
            "schema_version": 1,
            "delta_id": delta_id,
            "capture_id": capture.capture_id,
            "episode_id": capture.episode_id,
            "task_id": capture.request.task_id,
            "action_id": capture.request.action_id,
            "action": capture.request.name,
            "read_only": capture.request.read_only,
            "input": self._redacted_request_input(capture.request),
            "target": str(capture.request.target) if capture.request.target else None,
            "result": redact_result(result),
            "pre_observation_id": capture.request.pre_observation_id,
            "post_observation_id": observation.observation_id if observation else None,
            "pre_observation_digest": capture.request.pre_observation_digest,
            "post_observation_digest": post_digest,
            "pre_target_id": capture.request.pre_target_id,
            "post_target_id": observation.active_target_id if observation else None,
            "pre_url": pre_url,
            "post_url": post_url,
            "cookie_changes": browser.cookie_changes if browser else (),
            "storage_events": storage_events,
            "network_span": network_span,
            "artifacts": [artifact_to_dict(item) for item in optional_artifacts],
            "warnings": warnings,
            "errors": errors,
            "human_interventions": evidence.human_interventions if evidence else (),
            "omissions": omissions,
            "started_at": capture.started_at,
            "finished_at": finished_at,
            "duration_ms": round(duration_ms, 3),
            "redaction_policy_version": episode.policy.redaction_policy_version,
            "complete": True,
        }
        encoded = canonical_json(payload)
        if len(encoded) > episode.policy.max_artifact_bytes:
            await self._finish_capture_slot(capture)
            self._publish_action_pointer(
                capture,
                {**payload, "complete": False, "errors": [*errors, "required_artifact_too_large"]},
            )
            raise ValueError("required action artifact exceeds capture byte bound")
        try:
            async with asyncio.timeout(episode.policy.capture_timeout):
                record = await self._artifacts.put(
                    encoded,
                    kind=ArtifactKind.ACTION,
                    media_type="application/json",
                    schema_version=1,
                    security_class=SecurityClass.REDACTED,
                    redaction_policy_version=episode.policy.redaction_policy_version,
                )
            self._append_network_events(capture.episode_id, network_events)
            self._publish_action_pointer(
                capture, {**payload, "record_content_id": record.content_id}
            )
        except BaseException:
            self._publish_action_pointer(
                capture,
                {**payload, "complete": False, "errors": [*errors, "required_capture_failed"]},
            )
            raise
        finally:
            await self._finish_capture_slot(capture)

        return StateDelta(
            delta_id=delta_id,
            episode_id=capture.episode_id,
            task_id=capture.request.task_id,
            action_id=capture.request.action_id,
            pre_observation_id=capture.request.pre_observation_id,
            post_observation_id=observation.observation_id if observation else None,
            outcome=result.status,
            pre_observation_digest=capture.request.pre_observation_digest,
            post_observation_digest=post_digest,
            pre_target_id=capture.request.pre_target_id,
            post_target_id=observation.active_target_id if observation else None,
            pre_url=pre_url,
            post_url=post_url,
            target_changed=(
                capture.request.pre_target_id is not None
                and observation is not None
                and capture.request.pre_target_id != observation.active_target_id
            ),
            url_changed=(
                capture.request.pre_url is not None
                and observation is not None
                and capture.request.pre_url != observation.url
            ),
            cookie_changes=browser.cookie_changes if browser else (),
            storage_events=storage_events,
            network_events=network_events,
            network_span=network_span,
            artifacts=(record, *optional_artifacts),
            warnings=warnings,
            errors=errors,
            human_interventions=evidence.human_interventions if evidence else (),
            omissions=omissions,
            started_at=capture.started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            record=record,
        )

    async def confirm_shutdown(self, lease: EpisodeLease, proof: CleanShutdownProof) -> None:
        episode = self._require_open(lease)
        if proof.episode_id != lease.episode_id:
            raise CheckpointError("shutdown proof belongs to another episode")
        self._verify_stopped_profile(episode.profile)
        episode.shutdown = proof

    async def checkpoint(self, lease: EpisodeLease, reason: str) -> CheckpointRef:
        return await self._seal(lease, reason)

    async def close_episode(self, lease: EpisodeLease, outcome: EpisodeOutcome) -> CheckpointRef:
        return await self._seal(lease, f"terminal:{outcome}")

    async def abort_episode(self, lease: EpisodeLease, error: BaseException) -> DiagnosticRef:
        episode = self._take(lease)
        self._discard_captures(lease.episode_id)
        try:
            profile = await self._quarantine_profile(episode)
            diagnostic = await self._diagnostic(
                episode,
                phase="abort",
                error_type=type(error).__name__,
                quarantined_profile=profile,
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

    async def _finish_capture_slot(self, capture: ActionCapture) -> None:
        self._captures.pop(capture.capture_id, None)
        async with self._capture_condition:
            self._capture_condition.notify_all()

    async def _capture_optional_artifacts(
        self,
        episode: _Episode,
        observation: Observation | None,
        network_events: tuple[object, ...],
    ) -> tuple[tuple[ArtifactRef, ...], tuple[str, ...], tuple[str, ...]]:
        artifacts: list[ArtifactRef] = []
        warnings: list[str] = []
        omissions: list[str] = []
        payloads: list[tuple[ArtifactKind, bytes]] = []
        for kind in episode.policy.optional_artifacts:
            if kind is ArtifactKind.OBSERVATION and observation is not None:
                payloads.append((kind, canonical_json(observation_payload(observation))))
            elif kind is ArtifactKind.NETWORK:
                payloads.append((kind, canonical_json(network_events)))
            elif kind is ArtifactKind.SCREENSHOT:
                # Pixel redaction needs a field-region mask. Raw pixels are never
                # persisted when that mask is unavailable.
                warnings.append("optional_screenshot_redaction_unavailable")
                omissions.append("screenshot:redaction_unavailable")
            elif kind is ArtifactKind.DOM:
                # Action results can contain arbitrary page text. Semantic
                # observation capture above is safe; raw DOM remains omitted.
                warnings.append("optional_dom_redaction_unavailable")
                omissions.append("dom:redaction_unavailable")
            elif kind not in {ArtifactKind.ACTION, ArtifactKind.CHECKPOINT}:
                warnings.append(f"optional_{kind.value}_unsupported")
                omissions.append(f"{kind.value}:unsupported")

        for kind, data in payloads:
            if len(data) > episode.policy.max_artifact_bytes:
                warnings.append(f"optional_{kind.value}_too_large")
                omissions.append(f"{kind.value}:byte_bound")
                continue
            try:
                async with asyncio.timeout(episode.policy.capture_timeout):
                    artifacts.append(
                        await self._artifacts.put(
                            data,
                            kind=kind,
                            media_type="application/json",
                            schema_version=1,
                            security_class=SecurityClass.REDACTED,
                            redaction_policy_version=episode.policy.redaction_policy_version,
                        )
                    )
            except Exception as error:
                warnings.append(f"optional_{kind.value}_failed:{type(error).__name__}")
                omissions.append(f"{kind.value}:capture_failed")
        return tuple(artifacts), tuple(warnings), tuple(omissions)

    @staticmethod
    def _redacted_request_input(request: ActionRequest) -> object:
        return redact_action_input(
            BrowserAction(
                request.name,
                request.redacted_input,
                target=request.target,
                read_only=request.read_only,
            )
        )

    def _episode_for_id(self, episode_id: str) -> _Episode:
        for episode in self._open.values():
            if episode.lease.episode_id == episode_id:
                return episode
        raise LeaseClosedError("episode is not open")

    def _publish_action_pointer(self, capture: ActionCapture, payload: object) -> None:
        directory = self._action_records / capture.episode_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{capture.capture_id}.json"
        self._atomic_publish(canonical_json(payload), destination)

    def _append_network_events(self, episode_id: str, events: tuple[object, ...]) -> None:
        if not events:
            return
        destination = self._network_streams / f"{episode_id}.jsonl"
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(descriptor, "ab") as stream:
                for event in events:
                    stream.write(canonical_json(event) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            raise

    @staticmethod
    def _atomic_publish(data: bytes, destination: Path) -> None:
        temporary = destination.with_name(f".{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

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
            if episode.shutdown is None:
                raise CheckpointError("episode has no clean shutdown proof")
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
                shutdown=episode.shutdown,
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
                if profile_ref is None:
                    profile_ref = await self._quarantine_profile(episode)
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

    async def _quarantine_profile(self, episode: _Episode) -> ArtifactRef | None:
        if not episode.policy.restricted_storage:
            return None
        try:
            data, _files = archive_profile(episode.profile, reject_symlinks=False)
            return await self._artifacts.put(
                data,
                kind=ArtifactKind.DIAGNOSTIC,
                media_type="application/x-tar",
                schema_version=1,
                security_class=SecurityClass.RESTRICTED,
                redaction_policy_version=episode.policy.redaction_policy_version,
            )
        except Exception:
            return None

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
