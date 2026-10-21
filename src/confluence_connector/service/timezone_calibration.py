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
    """Measure a stable sample, retrying once if it changes; never persist an unverified offset."""
    for _ in range(2):
        try:
            return await _calibrate_once(api, username)
        except _SampleChanged:
            continue
    raise Upstream("The calibration sample changed during measurement; no timezone was measured. Try again.")


class _SampleChanged(Exception):
    """The source timestamp changed while the binary search was running."""


async def _calibrate_once(api: ConfluenceApi, username: str) -> CqlTimezoneCalibration:
    """Find a readable page, then binary-search its inclusive CQL lastmodified cutoff.

    The CLI calls this only after token verification in auth or status --check. It performs bounded
    read-only searches and never returns or logs page identity or content.
    """
    page = await _reference_page(api, username)
    page_id = page.content_id
    if page_id is None or not page_id.isdecimal():
        raise Upstream("Confluence did not return a usable page for timezone calibration.")
    sample = await api.get_page_metadata(page_id, expected_username=username)
    reference_utc = _modification_instant(sample.modified_at)
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
    verified = await api.get_page_metadata(page_id, expected_username=username)
    if _modification_instant(verified.modified_at) != reference_utc:
        raise _SampleChanged
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


def _modification_instant(value: str | None) -> datetime:
    """Parse one REST modification instant; equivalent timezone spellings identify the same sample."""
    if value is None:
        raise Upstream("Confluence did not return a page modification timestamp for calibration.")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Upstream("Confluence returned an invalid page modification timestamp.") from None
    if instant.tzinfo is None:
        raise Upstream("Confluence returned a modification timestamp without a timezone.")
    return instant.astimezone(timezone.utc)
