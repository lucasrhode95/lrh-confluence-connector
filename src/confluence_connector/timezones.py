"""Validated timezone names and minute-offset formatting used by CQL date filters."""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_FIXED_OFFSET = re.compile(r"UTC([+-])(\d{2}):(\d{2})\Z")
MAX_FIXED_OFFSET_MINUTES = 14 * 60


def parse_cql_timezone(value: str) -> tzinfo:
    """Return an IANA or supported fixed-offset timezone; input is validated at config/search edges."""
    if not isinstance(value, str) or not value:
        raise ValueError("cql_timezone must be an IANA name or UTC±HH:MM fixed offset.")
    fixed = _FIXED_OFFSET.fullmatch(value)
    if fixed is not None:
        hours, minutes = int(fixed.group(2)), int(fixed.group(3))
        if minutes > 59 or hours > 14 or (hours == 14 and minutes != 0):
            raise ValueError("cql_timezone fixed offset must be between UTC-14:00 and UTC+14:00.")
        offset_minutes = hours * 60 + minutes
        if fixed.group(1) == "-":
            offset_minutes = -offset_minutes
        return timezone(timedelta(minutes=offset_minutes))
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("cql_timezone must be a valid IANA zone or UTC±HH:MM fixed offset.") from None


def fixed_offset_name(offset_minutes: int) -> str:
    """Format a measured whole-minute offset as a stable private-config value."""
    if type(offset_minutes) is not int or abs(offset_minutes) > MAX_FIXED_OFFSET_MINUTES:
        raise ValueError("Timezone offset is outside the supported range.")
    sign = "+" if offset_minutes >= 0 else "-"
    hours, minutes = divmod(abs(offset_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def offset_minutes_at(zone: tzinfo, instant: datetime) -> int:
    """Return a whole-minute offset for an aware instant, rejecting historical second offsets."""
    if instant.tzinfo is None:
        raise ValueError("The instant must include a timezone.")
    offset = instant.astimezone(zone).utcoffset()
    if offset is None or offset.total_seconds() % 60:
        raise ValueError("The timezone offset is not representable in CQL minute precision.")
    return int(offset.total_seconds() // 60)
