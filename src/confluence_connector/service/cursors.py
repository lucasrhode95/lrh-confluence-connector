"""Opaque, self-contained cursors for process-independent read pagination."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from confluence_connector.domain.errors import InvalidRequest

MAX_CURSOR_CHARS = 16_384
_CURSOR_TEXT = re.compile(r"^[A-Za-z0-9_-]+$")


def encode(kind: str, **state: Any) -> str:
    """Encode validated request state; callers revalidate it when a cursor is used."""
    payload = json.dumps({"kind": kind, **state}, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode(cursor: str, kind: str) -> dict[str, Any]:
    """Decode untrusted cursor text and verify its capability kind before returning its state."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_CHARS or not _CURSOR_TEXT.fullmatch(cursor):
        raise InvalidRequest("Malformed cursor.")
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise InvalidRequest("Malformed cursor.") from None
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise InvalidRequest(f"This cursor does not belong to {kind}.")
    value.pop("kind")
    return value
