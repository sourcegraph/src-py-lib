"""Small JSON type aliases and projection helpers."""

from __future__ import annotations

from typing import Any, cast

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JSONDict = dict[str, JSONValue]
type JSONArray = list[JSONValue]


def json_dict(value: object) -> JSONDict:
    """Return `value` as a JSON object, or an empty object when it is not one."""
    return cast(JSONDict, value) if isinstance(value, dict) else {}


def json_list(value: object) -> JSONArray:
    """Return `value` as a JSON array, or an empty array when it is not one."""
    return cast(JSONArray, value) if isinstance(value, list) else []


def json_str(mapping: JSONDict, key: str, default: str = "") -> str:
    """Read a string value from a JSON object."""
    value = mapping.get(key)
    return value if isinstance(value, str) else default


def json_int(mapping: JSONDict, key: str, default: int = 0) -> int:
    """Read an integer value from a JSON object, excluding booleans."""
    value = mapping.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def require_json_dict(value: Any, *, where: str) -> JSONDict:
    """Return `value` as a JSON object, or raise a clear error."""
    if isinstance(value, dict):
        return cast(JSONDict, value)
    raise TypeError(f"{where} must be a JSON object")
