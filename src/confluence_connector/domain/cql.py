"""CQL literal formatting shared by search and remote route builders."""

from __future__ import annotations


def cql_literal(value: str) -> str:
    """Escape a validated text value as a single quoted CQL literal."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
