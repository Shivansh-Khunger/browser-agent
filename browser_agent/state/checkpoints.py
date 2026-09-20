"""Checkpoint archive and manifest codec for local browser state."""

from __future__ import annotations

import hashlib
import io
import json
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
    EpisodeMetadata,
    SecurityClass,
)


def archive_profile(root: Path) -> tuple[bytes, list[dict[str, object]]]:
    files = _file_manifest(root)
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
) -> bytes:
    manifest = {
        "schema_version": 1,
        "episode_id": episode_id,
        "parent_id": parent_id,
        "reason": reason,
        "clean_shutdown": True,
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
