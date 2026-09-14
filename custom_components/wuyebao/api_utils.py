"""Pure helpers for the WuyeBao API client (no Home Assistant imports).

These functions are deliberately dependency-free so they can be unit tested
outside of a Home Assistant runtime.
"""

from __future__ import annotations

from typing import Any

# Keys commonly used by Chinese app backends to carry an auth token.
TOKEN_KEY_CANDIDATES = (
    "token",
    "accessToken",
    "access_token",
    "access_token_value",
    "accessTokenValue",
    "authToken",
    "loginToken",
    "sessionToken",
)

# Keys that often hold the device / door list.
DEVICE_LIST_KEY_CANDIDATES = (
    "devices",
    "deviceList",
    "doors",
    "doorList",
    "list",
    "rows",
    "items",
    "data",
    "result",
)

IDENTITY_FIELD_CANDIDATES = (
    "id",
    "deviceId",
    "device_id",
    "doorId",
    "door_id",
    "serialNo",
    "sn",
    "code",
)

NAME_FIELD_CANDIDATES = (
    "name",
    "deviceName",
    "device_name",
    "doorName",
    "door_name",
    "alias",
    "title",
)

_MAX_DEPTH = 8


def find_token(payload: Any, depth: int = 0) -> str | None:
    """Search for a token anywhere in a JSON payload."""
    if payload is None or depth > _MAX_DEPTH:
        return None
    if isinstance(payload, dict):
        for key in TOKEN_KEY_CANDIDATES:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = find_token(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_token(value, depth + 1)
            if found:
                return found
    return None


def find_devices(payload: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """Search for a list of device objects anywhere in a JSON payload."""
    if payload is None or depth > _MAX_DEPTH:
        return None
    if isinstance(payload, dict):
        for key in DEVICE_LIST_KEY_CANDIDATES:
            value = payload.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
            if isinstance(value, dict):
                found = find_devices(value, depth + 1)
                if found:
                    return found
        for value in payload.values():
            found = find_devices(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return payload
        for value in payload:
            found = find_devices(value, depth + 1)
            if found:
                return found
    return None


def pick_field(device: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """Return the first candidate key that has a usable value."""
    for key in candidates:
        if key in device:
            value = device[key]
            if value is not None and str(value) != "":
                return value
    return None


def normalize_devices(
    devices: list[dict[str, Any]],
    id_field: str | None = None,
    name_field: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize a raw device list into [{device_id, name, raw}]."""
    normalized: list[dict[str, Any]] = []
    for raw in devices:
        if not isinstance(raw, dict):
            continue
        device_id = (
            raw.get(id_field) if id_field else pick_field(raw, IDENTITY_FIELD_CANDIDATES)
        )
        name = (
            raw.get(name_field)
            if name_field
            else pick_field(raw, NAME_FIELD_CANDIDATES)
        )
        if device_id is None:
            continue
        device_id = str(device_id)
        name = (
            raw.get(name_field)
            if name_field
            else pick_field(raw, NAME_FIELD_CANDIDATES)
        )
        normalized.append(
            {
                "device_id": device_id,
                "name": str(name or f"门禁 {device_id}"),
                "raw": raw,
            }
        )
    return normalized


def build_login_payload(phone: str, password: str) -> dict[str, str]:
    """Build the JSON body for the login call (common shape)."""
    return {"phone": phone, "password": password}


def build_open_payload(device_id: str, id_field: str) -> dict[str, str]:
    """Build the JSON body / query for the open-door call."""
    return {id_field: device_id}


def build_auth_headers(token: str, scheme: str) -> dict[str, str]:
    """Build the Authorization header, e.g. 'Bearer <token>'."""
    if scheme:
        return {"Authorization": f"{scheme} {token}"}
    return {"Authorization": token}
