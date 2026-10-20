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
