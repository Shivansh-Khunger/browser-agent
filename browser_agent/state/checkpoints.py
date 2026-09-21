"""Checkpoint archive and manifest codec for local browser state."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tarfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from .models import (
    ArtifactKind,
    ArtifactRef,
    CapturePolicy,
    CheckpointError,
    CheckpointRef,
    CleanShutdownProof,
    EpisodeMetadata,
    SecurityClass,
)

CHECKPOINT_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def archive_profile(
    root: Path, *, reject_symlinks: bool = True
) -> tuple[bytes, list[dict[str, object]]]:
    files = _file_manifest(root)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                if reject_symlinks:
                    raise CheckpointError("profile symlinks are not supported")
                continue
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
    return buffer.getvalue(), files


def extract_profile(data: bytes, destination: Path) -> None:
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


def encode_manifest(
    *,
    episode_id: str,
    parent_id: str | None,
    reason: str,
    profile: ArtifactRef,
    files: list[dict[str, object]],
    policy: CapturePolicy,
    metadata: EpisodeMetadata,
    shutdown: CleanShutdownProof,
) -> bytes:
    manifest = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "episode_id": episode_id,
        "parent_id": parent_id,
        "reason": reason,
        "clean_shutdown": True,
        "clean_shutdown_proof": {
            "episode_id": shutdown.episode_id,
            "process_id": shutdown.process_id,
            "exit_code": shutdown.exit_code,
        },
        "created_at": datetime.now(UTC).isoformat(),
        "profile": artifact_to_dict(profile),
        "files": files,
        "policy": {
            "version": policy.version,
            "redaction_policy_version": policy.redaction_policy_version,
            "max_queue_items": policy.max_queue_items,
            "max_artifact_bytes": policy.max_artifact_bytes,
            "capture_timeout": policy.capture_timeout,
            "restricted_storage": policy.restricted_storage,
            "optional_artifacts": sorted(item.value for item in policy.optional_artifacts),
            "retention_labels": list(policy.retention_labels),
            "incompatible_nodriver_transitions": [
                list(item) for item in sorted(policy.incompatible_nodriver_transitions)
            ],
        },
        "metadata": asdict(metadata),
    }
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def decode_manifest(data: bytes) -> dict[str, object]:
    try:
        raw: object = json.loads(data.decode())
    except Exception as error:
        raise CheckpointError("checkpoint manifest cannot be read") from error
    return _object_dict(raw, "checkpoint manifest is not an object")


def validate_manifest(
    manifest: dict[str, object],
    checkpoint: CheckpointRef,
    policy: CapturePolicy,
    metadata: EpisodeMetadata,
) -> tuple[str, ...]:
    """Validate restore authority and current-runtime compatibility."""
    if _integer(manifest.get("schema_version")) != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError("checkpoint schema version is unsupported")
    if checkpoint.schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError("checkpoint reference schema version is unsupported")
    if checkpoint.manifest is None or checkpoint.checkpoint_id != checkpoint.manifest.content_id:
        raise CheckpointError("checkpoint reference does not identify its manifest")
    if (
        checkpoint.manifest.kind is not ArtifactKind.CHECKPOINT
        or checkpoint.manifest.security_class is not SecurityClass.RESTRICTED
        or checkpoint.manifest.media_type != "application/json"
        or checkpoint.manifest.schema_version != CHECKPOINT_SCHEMA_VERSION
    ):
        raise CheckpointError("checkpoint manifest is not encrypted restore authority")

    episode_id = _required_string(manifest.get("episode_id"), "checkpoint episode is invalid")
    parent_id = _optional_string(manifest.get("parent_id"))
    reason = _required_string(manifest.get("reason"), "checkpoint reason is invalid")
    if (episode_id, parent_id, reason) != (
        checkpoint.episode_id,
        checkpoint.parent_id,
        checkpoint.reason,
    ):
        raise CheckpointError("checkpoint reference does not match manifest lineage")
    if not checkpoint.clean_shutdown or not _boolean(manifest.get("clean_shutdown")):
        raise CheckpointError("checkpoint lacks clean shutdown authority")
    proof = _object_dict(
        manifest.get("clean_shutdown_proof"), "checkpoint shutdown proof is invalid"
    )
    if (
        _required_string(proof.get("episode_id"), "checkpoint shutdown proof is invalid")
        != episode_id
        or _integer(proof.get("process_id")) <= 0
        or _integer(proof.get("exit_code")) != 0
    ):
        raise CheckpointError("checkpoint shutdown proof is invalid")

    profile = profile_reference(manifest)
    if (
        profile.kind is not ArtifactKind.CHECKPOINT
        or profile.security_class is not SecurityClass.RESTRICTED
        or profile.media_type != "application/x-tar"
        or profile.schema_version != CHECKPOINT_SCHEMA_VERSION
    ):
        raise CheckpointError("checkpoint profile is not encrypted restore authority")
    _validated_files(manifest.get("files"))

    writer = metadata_from_manifest(manifest)
    if _platform_family(writer.platform) != _platform_family(metadata.platform):
        raise CheckpointError("checkpoint platform is incompatible")
    writer_major = _browser_major(writer.browser_version)
    current_major = _browser_major(metadata.browser_version)
    if writer_major is not None and current_major is None:
        raise CheckpointError("current browser version cannot be validated")
    if writer_major is not None and current_major is not None and current_major < writer_major:
        raise CheckpointError("browser downgrade cannot restore checkpoint")

    warnings: list[str] = []
    transition = (writer.nodriver_version or "", metadata.nodriver_version or "")
    if all(transition) and transition[0] != transition[1]:
        if transition in policy.incompatible_nodriver_transitions:
            raise CheckpointError("nodriver version transition is configured incompatible")
        warnings.append(f"nodriver_version_changed:{transition[0]}->{transition[1]}")
    return tuple(warnings)


def verify_materialized_profile(root: Path, manifest: dict[str, object]) -> None:
    expected = _validated_files(manifest.get("files"))
    actual = {
        path.relative_to(root).as_posix(): (len(data), hashlib.sha256(data).hexdigest())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
        for data in (path.read_bytes(),)
    }
    if actual != expected:
        raise CheckpointError("checkpoint profile does not match file manifest")


def profile_reference(manifest: dict[str, object]) -> ArtifactRef:
    value = _object_dict(manifest.get("profile"), "checkpoint profile reference is invalid")
    try:
        return ArtifactRef(
            content_id=str(value["content_id"]),
            kind=ArtifactKind(str(value["kind"])),
            media_type=str(value["media_type"]),
            schema_version=_integer(value["schema_version"]),
            encoding=str(value["encoding"]),
            compression=str(value["compression"]) if value["compression"] else None,
            byte_length=_integer(value["byte_length"]),
            security_class=SecurityClass(str(value["security_class"])),
            redaction_policy_version=str(value["redaction_policy_version"]),
            restricted_locator=(
                str(value["restricted_locator"]) if value["restricted_locator"] else None
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CheckpointError("checkpoint profile reference is invalid") from error


def policy_from_manifest(manifest: dict[str, object]) -> CapturePolicy:
    value = _object_dict(manifest.get("policy"), "checkpoint policy is invalid")
    try:
        return CapturePolicy(
            version=str(value["version"]),
            redaction_policy_version=str(value["redaction_policy_version"]),
            optional_artifacts=frozenset(
                ArtifactKind(item)
                for item in _string_list(
                    value.get("optional_artifacts"), "checkpoint policy is invalid"
                )
            ),
            max_queue_items=_integer(value["max_queue_items"]),
            max_artifact_bytes=_integer(value["max_artifact_bytes"]),
            capture_timeout=_number(value["capture_timeout"]),
            restricted_storage=_boolean(value["restricted_storage"]),
            retention_labels=tuple(
                _string_list(value.get("retention_labels"), "checkpoint policy is invalid")
            ),
            incompatible_nodriver_transitions=frozenset(
                _version_transition(item)
                for item in _object_list(
                    value.get("incompatible_nodriver_transitions"),
                    "checkpoint policy is invalid",
                )
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CheckpointError("checkpoint policy is invalid") from error


def metadata_from_manifest(manifest: dict[str, object]) -> EpisodeMetadata:
    value = _object_dict(manifest.get("metadata"), "checkpoint metadata is invalid")
    try:
        return EpisodeMetadata(
            code_revision=str(value["code_revision"]),
            platform=str(value["platform"]),
            task_id=_optional_string(value.get("task_id")),
            browser_executable=_optional_string(value.get("browser_executable")),
            browser_version=_optional_string(value.get("browser_version")),
            nodriver_version=_optional_string(value.get("nodriver_version")),
            config_digest=_optional_string(value.get("config_digest")),
            locale_override=_optional_string(value.get("locale_override")),
            timezone_override=_optional_string(value.get("timezone_override")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CheckpointError("checkpoint metadata is invalid") from error


def artifact_to_dict(reference: ArtifactRef) -> dict[str, object]:
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


def _object_dict(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CheckpointError(message)
    untyped = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in untyped):
        raise CheckpointError(message)
    return cast(dict[str, object], value)


def _object_list(value: object, message: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CheckpointError(message)
    return cast(list[object], value)


def _string_list(value: object, message: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CheckpointError(message)
    untyped = cast(list[object], value)
    if not all(isinstance(item, str) for item in untyped):
        raise CheckpointError(message)
    return cast(list[str], value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CheckpointError("checkpoint metadata is invalid")
    return value


def _required_string(value: object, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise CheckpointError(message)
    return value


def _version_transition(value: object) -> tuple[str, str]:
    if not isinstance(value, list):
        raise CheckpointError("checkpoint policy is invalid")
    items = cast(list[object], value)
    if len(items) != 2 or not all(isinstance(item, str) and item for item in items):
        raise CheckpointError("checkpoint policy is invalid")
    return cast(str, items[0]), cast(str, items[1])


def _validated_files(value: object) -> dict[str, tuple[int, str]]:
    files: dict[str, tuple[int, str]] = {}
    for raw in _object_list(value, "checkpoint file manifest is invalid"):
        item = _object_dict(raw, "checkpoint file manifest is invalid")
        path = _required_string(item.get("path"), "checkpoint file manifest is invalid")
        relative = PurePosixPath(path)
        size = _integer(item.get("bytes"))
        digest = _required_string(item.get("sha256"), "checkpoint file manifest is invalid")
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or path in files
            or size < 0
            or not _SHA256.fullmatch(digest)
        ):
            raise CheckpointError("checkpoint file manifest is invalid")
        files[path] = (size, digest)
    return files


def _platform_family(value: str) -> str:
    return value.split("-", 1)[0].lower()


def _browser_major(value: str | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CheckpointError("checkpoint numeric value is invalid")
    return value


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CheckpointError("checkpoint numeric value is invalid")
    return float(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise CheckpointError("checkpoint boolean value is invalid")
    return value
