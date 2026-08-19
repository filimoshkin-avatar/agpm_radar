"""Shared recursive JSON types without importing any runtime domain component."""

from __future__ import annotations

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

__all__ = ["JsonObject", "JsonScalar", "JsonValue"]
