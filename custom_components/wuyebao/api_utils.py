"""Pure helpers for the JHCloud WuyeBao API client (no Home Assistant imports).

These functions are deliberately dependency-free so they can be unit tested
outside of a Home Assistant runtime.
"""

from __future__ import annotations

from typing import Any

try:  # normal package import inside Home Assistant
    from .const import GATE_ID_FIELD_CANDIDATES, GATE_NAME_FIELD_CANDIDATES
except ImportError:  # standalone unit-test import
    from const import GATE_ID_FIELD_CANDIDATES, GATE_NAME_FIELD_CANDIDATES

# --------------------------------------------------------------------------
# Response envelope
# --------------------------------------------------------------------------
# JHCloud responses look like {"code":0,"data":...} on success and
# {"code":1003,"data":"帐号或密码错误！"} / {"code":1001,"data":"无效Token！"}
# on failure. HTTP status follows: 200 on success, 400/401/500 on failure.
_SUCCESS_CODES = (0, "0", "0000", "success", "SUCCESS", 200)


def is_success(payload: dict[str, Any] | None) -> bool:
    """True when the JHCloud envelope reports success (or has no code field)."""
    if not isinstance(payload, dict):
        return True
    code = payload.get("code")
    if code is None:
        # No envelope at all: treat as raw success payload.
        return True
    if isinstance(code, str):
        return code.strip() in _SUCCESS_CODES or code.strip().lower() == "success"
    try:
        return int(code) == 0
    except (TypeError, ValueError):
        return False


def get_data(payload: dict[str, Any] | None) -> Any:
    """Return the envelope's data field (or the payload itself)."""
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


# --------------------------------------------------------------------------
# Token extraction
# --------------------------------------------------------------------------
TOKEN_KEY_CANDIDATES = (
    "accessToken",
    "access_token",
    "token",
    "authToken",
    "loginToken",
    "sessionToken",
)

REFRESH_TOKEN_KEY_CANDIDATES = (
    "refreshToken",
    "refresh_token",
)


def find_token(payload: Any, depth: int = 0) -> str | None:
    """Find the access token anywhere inside a payload."""
    return _find_key(payload, TOKEN_KEY_CANDIDATES, depth)


def find_refresh_token(payload: Any, depth: int = 0) -> str | None:
    """Find the refresh token anywhere inside a payload."""
    return _find_key(payload, REFRESH_TOKEN_KEY_CANDIDATES, depth)


def _find_key(payload: Any, candidates: tuple[str, ...], depth: int) -> str | None:
    if payload is None or depth > 8:
        return None
    if isinstance(payload, dict):
        for key in candidates:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _find_key(value, candidates, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_key(value, candidates, depth + 1)
            if found:
                return found
    return None


# --------------------------------------------------------------------------
# Gate list handling
# --------------------------------------------------------------------------
GATE_LIST_KEY_CANDIDATES = (
    "gates",
    "gateList",
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


def find_gates(payload: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """Search for a list of gate objects anywhere in a JSON payload."""
    if payload is None or depth > 8:
        return None
    if isinstance(payload, dict):
        for key in GATE_LIST_KEY_CANDIDATES:
            value = payload.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
            if isinstance(value, dict):
                found = find_gates(value, depth + 1)
                if found:
                    return found
        for value in payload.values():
            found = find_gates(value, depth + 1)
            if found:
                return found
    elif isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            return payload
        for value in payload:
            found = find_gates(value, depth + 1)
            if found:
                return found
    return None


def pick_field(obj: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """Return the first candidate key that has a usable value."""
    for key in candidates:
        if key in obj:
            value = obj[key]
            if value is not None and str(value) != "":
                return value
    return None


def build_gate_display_name(raw: dict[str, Any], fallback_id: str) -> str:
    """Build a human-friendly gate name from available fields."""
    named = pick_field(raw, GATE_NAME_FIELD_CANDIDATES)
    if named:
        return str(named)
    parts = []
    for key in (
        "communityName",
        "community",
        "buildingName",
        "building",
        "unitName",
        "unit",
        "floorName",
        "floor",
        "roomName",
        "room",
        "areaName",
    ):
        value = raw.get(key)
        if value is not None and str(value) not in ("", "0", "null"):
            parts.append(str(value))
    if parts:
        return " ".join(parts)
    return f"门禁 {fallback_id}"


def normalize_gates(
    gates: list[dict[str, Any]],
    id_field: str | None = None,
    name_field: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize a raw gate list into [{gate_id, name, raw}]."""
    normalized: list[dict[str, Any]] = []
    for raw in gates:
        if not isinstance(raw, dict):
            continue
        gate_id = (
            raw.get(id_field) if id_field else pick_field(raw, GATE_ID_FIELD_CANDIDATES)
        )
        if gate_id is None:
            continue
        gate_id = str(gate_id)
        if name_field and raw.get(name_field):
            name = str(raw.get(name_field))
        else:
            name = build_gate_display_name(raw, gate_id)
        normalized.append({"gate_id": gate_id, "name": name, "raw": raw})
    return normalized


# --------------------------------------------------------------------------
# Request builders
# --------------------------------------------------------------------------
def build_login_payload(phone: str, password: str) -> dict[str, str]:
    """Build the JSON body for /api/client/anon/token."""
    return {"username": phone, "password": password}


def build_auth_headers(token: str) -> dict[str, str]:
    """Build the Authorization header for grant endpoints."""
    return {"Authorization": f"Bearer {token}"}


def build_open_path(path_template: str, gate_id: str) -> str:
    """Substitute {gateId} (and aliases) in the open-door path template."""
    path = path_template or ""
    path = path.replace("{gateId}", gate_id)
    path = path.replace("{gate_id}", gate_id)
    path = path.replace("{id}", gate_id)
    path = path.replace("{deviceId}", gate_id)
    return path
