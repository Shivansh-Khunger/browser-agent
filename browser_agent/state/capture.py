"""Canonical redaction and serialization for browser-action evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..browser.models import ActionResult, BrowserAction, NetworkEvent, Observation, StorageEvent

_SECRET_FIELDS = {
    "authorization",
    "cookie",
    "code",
    "credential",
    "cvv",
    "otp",
    "password",
    "secret",
    "set-cookie",
    "text",
    "token",
    "value",
}
_SENSITIVE_QUERY_FIELDS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "code",
    "key",
    "otp",
    "password",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def redact_url(value: str) -> str:
    try:
        split = urlsplit(value)
        query = []
        for key, item in parse_qsl(split.query, keep_blank_values=True):
            query.append((key, "[REDACTED]" if _sensitive_name(key) else item))
        hostname = split.hostname or ""
        if split.port is not None:
            hostname = f"{hostname}:{split.port}"
        return urlunsplit((split.scheme, hostname, split.path, urlencode(query), ""))
    except ValueError:
        return "[REDACTED_URL]"


def redact_headers(headers: Mapping[str, object]) -> tuple[dict[str, str], int]:
    safe: dict[str, str] = {}
    removed = 0
    for name, value in headers.items():
        if _sensitive_name(name):
            removed += 1
        else:
            safe[str(name).lower()] = str(value)
    return safe, removed


def redact_action_input(action: BrowserAction) -> Mapping[str, object]:
    """Never retain raw typed, form, OTP, password, or secret-like values."""

    redacted: dict[str, object] = {}
    if action.name == "fill_form":
        fields = action.arguments.get("fields")
        safe_fields: list[object] = []
        if isinstance(fields, list | tuple):
            for field in fields:
                if isinstance(field, Mapping):
                    if field.get("placeholder") == "[REDACTED]":
                        safe_fields.append(
                            {
                                "target": str(field.get("target", "")),
                                "character_count": int(field.get("character_count", 0)),
                                "placeholder": "[REDACTED]",
                                "sensitivity": "form_value",
                            }
                        )
                        continue
                    raw = field.get("text", field.get("value", ""))
                    safe_fields.append(
                        {
                            "target": str(field.get("target", "")),
                            "character_count": len(raw) if isinstance(raw, str) else 0,
                            "placeholder": "[REDACTED]",
                            "sensitivity": "form_value",
                        }
                    )
        return {"fields": safe_fields, "submit": bool(action.arguments.get("submit", False))}

    for key, value in action.arguments.items():
        lowered = key.casefold()
        if isinstance(value, Mapping) and value.get("placeholder") == "[REDACTED]":
            redacted[key] = redact_value(value)
            continue
        if action.name in {"type", "type_otp"} or _sensitive_name(lowered):
            redacted[key] = _placeholder(value, action.name)
        elif lowered == "url" and isinstance(value, str):
            redacted[key] = redact_url(value)
        elif lowered in {"direction", "label", "observation_id", "screenshot_id"}:
            redacted[key] = redact_text(str(value))
        elif isinstance(value, str):
            redacted[key] = _placeholder(value, "unclassified_text")
        else:
            redacted[key] = redact_value(value, field_name=key)
    return redacted


def redact_result(result: ActionResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "message": redact_text(result.message),
        "error_code": result.error_code,
        "retryable": result.retryable,
        "details": _redact_result_details(result.details),
    }


def redact_value(value: object, *, field_name: str | None = None) -> object:
    if field_name and _sensitive_name(field_name):
        return _placeholder(value, field_name)
    if isinstance(value, str):
        if field_name and field_name.casefold() == "url":
            return redact_url(value)
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(key): redact_value(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


def redact_text(value: str) -> str:
    # Persisted logical text is bounded. Field-aware callers remove known secrets;
    # this bound prevents accidental whole-page/body persistence.
    return value[:8_000]


def observation_payload(observation: Observation) -> dict[str, object]:
    controls = []
    removed_values = 0
    for control in observation.controls:
        value = control.value
        if value is not None:
            removed_values += 1
            value = None
        controls.append(
            {
                "handle": str(control.handle),
                "role": control.role,
                "name": "[OMITTED]",
                "description": "[OMITTED]",
                "value": value,
                "filled": control.filled,
                "states": sorted(control.states),
                "frame": list(control.frame_breadcrumb),
                "sensitive": control.potentially_sensitive,
            }
        )
    return {
        "schema_version": 1,
        "observation_id": observation.observation_id,
        "active_target_id": observation.active_target_id,
        "url": redact_url(observation.url),
        "title": "[OMITTED]",
        "document_generation": observation.document_generation,
        "frame_generations": dict(observation.frame_generations),
        "controls": controls,
        "context": [
            {
                "kind": node.kind,
                "text": "[OMITTED]",
                "frame": list(node.frame_breadcrumb),
                "origin": node.frame_origin,
            }
            for node in observation.context
        ],
        "warnings": list(observation.warnings),
        "truncated": observation.truncated,
        "omitted_counts": dict(observation.omitted_counts),
        "redaction": {
            "removed_field_count": removed_values
            + (len(observation.controls) * 2)
            + len(observation.context)
            + 1,
            "removed_categories": ["dom_text", "form_value"],
        },
    }


def observation_digest(observation: Observation | None) -> str | None:
    if observation is None:
        return None
    return f"sha256:{hashlib.sha256(canonical_json(observation_payload(observation))).hexdigest()}"


def redact_network_event(event: NetworkEvent) -> NetworkEvent:
    headers, _removed = redact_headers(event.headers)
    return NetworkEvent(
        sequence=event.sequence,
        kind=event.kind,
        request_id=event.request_id,
        monotonic_time=event.monotonic_time,
        url=redact_url(event.url) if event.url else None,
        method=event.method,
        status=event.status,
        mime_type=event.mime_type,
        encoded_data_length=event.encoded_data_length,
        headers=headers,
    )


def redact_storage_event(event: StorageEvent) -> StorageEvent:
    return StorageEvent(
        event.sequence,
        event.kind,
        event.storage_type,
        event.origin,
        "[REDACTED]" if event.key and _sensitive_name(event.key) else event.key,
    )


def _sensitive_name(name: str) -> bool:
    lowered = name.casefold().replace("_", "-")
    return any(
        part in _SECRET_FIELDS or part in _SENSITIVE_QUERY_FIELDS for part in lowered.split("-")
    )


def _placeholder(value: object, sensitivity: str) -> dict[str, object]:
    length = len(value) if isinstance(value, (str, Mapping, list, tuple, set, frozenset)) else 0
    return {
        "placeholder": "[REDACTED]",
        "character_count": length,
        "sensitivity": sensitivity,
    }


def _redact_result_details(details: Mapping[str, object]) -> dict[str, object]:
    safe_scalars = {
        "action_status",
        "candidate_count",
        "characters_entered",
        "completed_count",
        "device_scale",
        "height",
        "mime_type",
        "offset_x",
        "offset_y",
        "scale",
        "scroll_x",
        "scroll_y",
        "width",
    }
    safe_lists = {"completed_fields"}
    redacted: dict[str, object] = {}
    for key, value in details.items():
        if key in safe_scalars and (value is None or isinstance(value, (str, bool, int, float))):
            redacted[key] = value
        elif key in safe_lists and isinstance(value, (list, tuple)):
            redacted[key] = [str(item) for item in value]
        elif key == "url" and isinstance(value, str):
            redacted[key] = redact_url(value)
        else:
            redacted[key] = _placeholder(value, "result_content")
    return redacted


def _json_value(value: object) -> Any:
    if is_dataclass(value):
        return {item.name: _json_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return repr(value)
