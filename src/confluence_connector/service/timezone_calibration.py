"""Measure CQL's effective local offset from a page's known UTC modification instant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from confluence_connector.domain.errors import Upstream
from confluence_connector.domain.models import ContentSearchHit
from confluence_connector.remote.confluence import ConfluenceApi

_REFERENCE_QUERY = "type = page ORDER BY lastmodified DESC"
_REFERENCE_LIMIT = 10
_SEARCH_BOUND_MINUTES = 15 * 60
_MAX_SUPPORTED_OFFSET_MINUTES = 14 * 60


@dataclass(frozen=True)
class CqlTimezoneCalibration:
    """Effective whole-minute offset and the UTC sample instant used to measure it."""

    offset_minutes: int
    reference_utc: datetime


async def calibrate_cql_timezone(api: ConfluenceApi, username: str) -> CqlTimezoneCalibration:
    """Find a readable page, then binary-search its inclusive CQL lastmodified cutoff.

    The CLI calls this only after token verification in auth or status --check. It performs bounded
    read-only searches and never returns or logs page identity or content.
    """
    page = await _reference_page(api, username)
    page_id = page.content_id
    if page_id is None or not page_id.isdecimal():
        raise Upstream("Confluence did not return a usable page for timezone calibration.")
    if page.modified_at is None:
        raise Upstream("Confluence did not return a page modification timestamp for calibration.")
    try:
        reference = datetime.fromisoformat(page.modified_at.replace("Z", "+00:00"))
    except ValueError:
        raise Upstream("Confluence returned an invalid page modification timestamp.") from None
    if reference.tzinfo is None:
        raise Upstream("Confluence returned a modification timestamp without a timezone.")
    reference_utc = reference.astimezone(timezone.utc)
    reference_minute = reference_utc.replace(second=0, microsecond=0)

    low, high = -_SEARCH_BOUND_MINUTES, _SEARCH_BOUND_MINUTES
    while high - low > 1:
        midpoint = (low + high) // 2
        cutoff = reference_minute + timedelta(minutes=midpoint)
        if await _matches_cutoff(api, username, page_id, cutoff):
            low = midpoint
        else:
            high = midpoint

    if abs(low) > _MAX_SUPPORTED_OFFSET_MINUTES:
        raise Upstream("Confluence's effective CQL timezone is outside the supported calibration range.")
    return CqlTimezoneCalibration(offset_minutes=low, reference_utc=reference_utc)


async def _reference_page(api: ConfluenceApi, username: str) -> ContentSearchHit:
    """Return the newest readable page hit with a numeric ID and UTC modification timestamp."""
    page = await api.search_content_page(
        _REFERENCE_QUERY,
        start=0,
        limit=_REFERENCE_LIMIT,
        include_archived=True,
        expected_username=username,
    )
    for hit in page.hits:
        if hit.content_id and hit.content_id.isdecimal() and hit.modified_at:
            return hit
    raise Upstream("No readable page with a UTC modification timestamp was found for calibration.")


async def _matches_cutoff(
    api: ConfluenceApi, username: str, page_id: str, cutoff: datetime
) -> bool:
    """Test one inclusive timezone-less CQL minute without exposing the page ID to the caller."""
    literal = cutoff.strftime("%Y/%m/%d %H:%M")
    page = await api.search_content_page(
        f"id = {page_id} AND lastmodified >= '{literal}'",
        start=0,
        limit=1,
        include_archived=True,
        expected_username=username,
    )
    return any(hit.content_id == page_id for hit in page.hits)
