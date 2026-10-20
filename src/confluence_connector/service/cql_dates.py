"""Normalize date scope values and format CQL-compatible minute boundaries."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Literal

from confluence_connector.domain.errors import InvalidRequest
from confluence_connector.timezones import parse_cql_timezone

DateBound = Literal["after", "before"]


def normalize_scope_time(name: str, value: str | None) -> str | None:
    """Validate an entry-point date value and store timestamps as canonical UTC instants."""
    if value is None:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise InvalidRequest(f"scope.{name} must be an ISO date or ISO-8601 timestamp.") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def date_range_is_inverted(after: str | None, before: str | None, cql_timezone: str | None) -> bool:
    """Compare normalized bounds as instants, treating a date-only bound as its local calendar day.

    Timestamp values are validated and normalized by ``normalize_scope_time``. Date-only values
    represent the complete local day, so mixed bounds compare the timestamp's local date to that day.
    If a mixed range has no timezone, defer validation because CQL construction will return the
    actionable missing-timezone error for its timestamp bound.
    """
    if after is None or before is None:
        return False

    after_is_date = len(after) == 10
    before_is_date = len(before) == 10
    if after_is_date and before_is_date:
        return date.fromisoformat(after) > date.fromisoformat(before)
    if not after_is_date and not before_is_date:
        after_instant = _parse_utc_timestamp(after)
        before_instant = _parse_utc_timestamp(before)
        return after_instant > before_instant
    if cql_timezone is None:
        return False

    zone = parse_cql_timezone(cql_timezone)
    if after_is_date:
        after_day = date.fromisoformat(after)
        before_day = _parse_utc_timestamp(before).astimezone(zone).date()
        return after_day > before_day

    after_day = _parse_utc_timestamp(after).astimezone(zone).date()
    before_day = date.fromisoformat(before)
    return after_day > before_day


def _parse_utc_timestamp(value: str) -> datetime:
    """Parse a normalized timestamp value as an aware UTC instant."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def cql_date_literal(
    name: str,
    value: str,
    bound: DateBound,
    cql_timezone: str | None,
) -> str:
    """Format a validated scope date as a Confluence CQL day or local minute literal.

    The search entry point stores timestamp bounds as UTC instants. CQL accepts only timezone-less
    local minutes, so timestamps require a configured timezone and are converted here for every page,
    including cursor continuations. Internal callers pass validated scope values.
    """
    if len(value) == 10:
        try:
            return date.fromisoformat(value).strftime("%Y/%m/%d")
        except ValueError:
            raise InvalidRequest(f"scope.{name} must be an ISO date.") from None
    if cql_timezone is None:
        raise InvalidRequest(
            "Timestamp filters require cql_timezone; run `confluence-connector status --check` to calibrate it."
        )
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        zone = parse_cql_timezone(cql_timezone)
    except (AttributeError, TypeError, ValueError):
        raise InvalidRequest(f"scope.{name} contains an invalid timestamp or cql_timezone.") from None

    local = moment.astimezone(zone)
    wall = local.replace(tzinfo=None, second=0, microsecond=0)
    if bound == "before" and (local.second or local.microsecond):
        wall += timedelta(minutes=1)

    candidates = _valid_wall_offsets(wall, zone)
    if len(candidates) == 2 and candidates[0] != candidates[1]:
        transition = abs(candidates[0] - candidates[1])
        wall += transition if bound == "before" else -transition
        candidates = _valid_wall_offsets(wall, zone)

    if not candidates:
        wall = _next_valid_wall_minute(wall, zone)
    return wall.strftime("%Y/%m/%d %H:%M")


def _valid_wall_offsets(wall: datetime, zone: tzinfo) -> list[timedelta]:
    """Return distinct offsets whose wall-time interpretation round-trips through UTC."""
    offsets: list[timedelta] = []
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold)
        offset = candidate.utcoffset()
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if offset is not None and round_trip == wall and offset not in offsets:
            offsets.append(offset)
    return offsets


def _next_valid_wall_minute(wall: datetime, zone: tzinfo) -> datetime:
    """Move a nonexistent local minute forward to the first representable wall minute."""
    for _ in range(24 * 60 + 1):
        if _valid_wall_offsets(wall, zone):
            return wall
        wall += timedelta(minutes=1)
    raise InvalidRequest("The timestamp falls inside an unsupported timezone transition gap.")
