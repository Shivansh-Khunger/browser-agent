"""Content-addressed artifact storage seam."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import ArtifactKind, ArtifactRef, SecurityClass


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
