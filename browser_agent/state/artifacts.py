"""Content-addressed artifact storage seam."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .models import (
    ArtifactIntegrityError,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactRef,
    RestrictedStorageError,
    SecurityClass,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ENCRYPTED_MAGIC = b"BAE1"


@runtime_checkable
class ArtifactStore(Protocol):
    async def put(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        media_type: str,
        schema_version: int,
        security_class: SecurityClass,
        redaction_policy_version: str,
    ) -> ArtifactRef: ...

    async def get(self, reference: ArtifactRef) -> bytes: ...


class LocalArtifactStore:
    """Atomic local content-addressed storage with encrypted restricted objects."""

    def __init__(self, root: Path, *, encryption_key: bytes | None = None) -> None:
        if encryption_key is not None and len(encryption_key) != 32:
            raise ValueError("encryption key must contain exactly 32 bytes")
        self._root = root
        self._key = encryption_key
        self._temporary = root / ".tmp"
        self._temporary.mkdir(parents=True, exist_ok=True, mode=0o700)

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
        if security_class is SecurityClass.RESTRICTED:
            return self._put_restricted(
                data, kind, media_type, schema_version, redaction_policy_version
            )
        return self._put_redacted(data, kind, media_type, schema_version, redaction_policy_version)

    async def get(self, reference: ArtifactRef) -> bytes:
        digest = self._digest(reference)
        path = self._object_path(reference.security_class, digest)
        try:
            stored = path.read_bytes()
        except FileNotFoundError as error:
            raise ArtifactNotFoundError(reference.content_id) from error

        if reference.security_class is SecurityClass.REDACTED:
            if not hmac.compare_digest(hashlib.sha256(stored).hexdigest(), digest):
                raise ArtifactIntegrityError(reference.content_id)
            return stored

        if self._key is None:
            raise RestrictedStorageError("restricted artifacts require an encryption key")
        if len(stored) < len(_ENCRYPTED_MAGIC) + 12 or not stored.startswith(_ENCRYPTED_MAGIC):
            raise ArtifactIntegrityError(reference.content_id)
        nonce = stored[len(_ENCRYPTED_MAGIC) : len(_ENCRYPTED_MAGIC) + 12]
        ciphertext = stored[len(_ENCRYPTED_MAGIC) + 12 :]
        try:
            plaintext = AESGCM(self._key).decrypt(nonce, ciphertext, self._aad(reference))
        except Exception as error:
            raise ArtifactIntegrityError(reference.content_id) from error
        expected = hmac.new(self._key, plaintext, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            raise ArtifactIntegrityError(reference.content_id)
        return plaintext

    def _put_redacted(
        self,
        data: bytes,
        kind: ArtifactKind,
        media_type: str,
        schema_version: int,
        redaction_policy_version: str,
    ) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        reference = self._reference(
            digest,
            "sha256",
            kind,
            media_type,
            schema_version,
            SecurityClass.REDACTED,
            redaction_policy_version,
            len(data),
        )
        self._publish(data, self._object_path(SecurityClass.REDACTED, digest))
        return reference

    def _put_restricted(
        self,
        data: bytes,
        kind: ArtifactKind,
        media_type: str,
        schema_version: int,
        redaction_policy_version: str,
    ) -> ArtifactRef:
        if self._key is None:
            raise RestrictedStorageError("restricted artifacts require an encryption key")
        digest = hmac.new(self._key, data, hashlib.sha256).hexdigest()
        reference = self._reference(
            digest,
            "hmac-sha256",
            kind,
            media_type,
            schema_version,
            SecurityClass.RESTRICTED,
            redaction_policy_version,
            len(data),
        )
        destination = self._object_path(SecurityClass.RESTRICTED, digest)
        if not destination.exists():
            nonce = secrets.token_bytes(12)
            encrypted = (
                _ENCRYPTED_MAGIC
                + nonce
                + AESGCM(self._key).encrypt(nonce, data, self._aad(reference))
            )
            self._publish(encrypted, destination)
        return reference

    def _reference(
        self,
        digest: str,
        algorithm: str,
        kind: ArtifactKind,
        media_type: str,
        schema_version: int,
        security_class: SecurityClass,
        redaction_policy_version: str,
        byte_length: int,
    ) -> ArtifactRef:
        return ArtifactRef(
            content_id=f"{algorithm}:{digest}",
            kind=kind,
            media_type=media_type,
            schema_version=schema_version,
            encoding="identity",
            compression=None,
            byte_length=byte_length,
            security_class=security_class,
            redaction_policy_version=redaction_policy_version,
            restricted_locator=(
                f"restricted/{digest[:2]}/{digest[2:]}"
                if security_class is SecurityClass.RESTRICTED
                else None
            ),
        )

    def _object_path(self, security_class: SecurityClass, digest: str) -> Path:
        bucket = "restricted" if security_class is SecurityClass.RESTRICTED else "redacted"
        return self._root / bucket / digest[:2] / digest[2:]

    def _publish(self, data: bytes, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._temporary / f"{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(FileExistsError):
                os.link(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _digest(reference: ArtifactRef) -> str:
        try:
            algorithm, digest = reference.content_id.split(":", 1)
        except ValueError as error:
            raise ArtifactIntegrityError(reference.content_id) from error
        expected = (
            "hmac-sha256" if reference.security_class is SecurityClass.RESTRICTED else "sha256"
        )
        if algorithm != expected or not _DIGEST.fullmatch(digest):
            raise ArtifactIntegrityError(reference.content_id)
        return digest

    @staticmethod
    def _aad(reference: ArtifactRef) -> bytes:
        return reference.content_id.encode()
