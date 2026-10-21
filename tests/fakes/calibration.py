"""Shared synthetic UTC page sample for timezone probes."""

from datetime import datetime, timezone

_REFERENCE_UTC = datetime(2026, 10, 19, 12, 34, 56, tzinfo=timezone.utc)
_PAGE_ID = "24680"


def _page_row() -> dict[str, object]:
    return {
        "content": {
            "id": _PAGE_ID,
            "type": "page",
            "title": "Synthetic calibration page",
            "version": {"when": _REFERENCE_UTC.isoformat().replace("+00:00", "Z")},
        }
    }

