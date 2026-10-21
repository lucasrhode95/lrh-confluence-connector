"""Shared validation rules for page references and space keys."""

from __future__ import annotations

import re
from typing import Any

from confluence_connector.domain.errors import InvalidRequest
from confluence_connector.domain.models import PageMetadata

MAX_PAGE_REFERENCE_LENGTH = 2_048
_SPACE_KEY = re.compile(r"^(?:[A-Za-z0-9_-]{1,255}|~[A-Za-z0-9_.@-]{1,254})$")


def page_reference(value: Any, *, field: str = "page") -> str:
    """Validate one untrusted public page reference and return its trimmed form."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > MAX_PAGE_REFERENCE_LENGTH
        or any(not character.isprintable() for character in value)
    ):
        if field == "page":
            raise InvalidRequest("Pass a non-empty page ID or supported same-instance page URL.")
        raise InvalidRequest(f"{field} must be a page ID or supported page URL.")
    return value.strip()


def page_references(value: Any, *, maximum: int, field: str = "pages") -> list[str]:
    """Validate one or more public references once before page resolution begins."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise InvalidRequest(f"{field} must be one page reference or a list of page references.")
    if not values or len(values) > maximum:
        raise InvalidRequest(f"{field} must contain 1 to {maximum} page references.")
    result: list[str] = []
    for item in values:
        try:
            result.append(page_reference(item, field=field))
        except InvalidRequest:
            raise InvalidRequest(f"{field} contains an invalid page reference.") from None
    return result


def is_space_key(value: Any) -> bool:
    """Return whether a value follows the shared normal or personal space-key form."""
    return isinstance(value, str) and _SPACE_KEY.fullmatch(value) is not None


def space_key(value: Any, *, field: str = "space_key") -> str:
    """Validate one untrusted space key at its public input boundary."""
    if not is_space_key(value):
        raise InvalidRequest(
            f"{field} must contain 1 to 255 letters, digits, underscores or hyphens, "
            "or a ~ personal-space key whose username may also contain dots and @."
        )
    return value


def optional_space_key(value: Any, *, field: str = "space_key") -> str | None:
    """Validate an optional untrusted space key, preserving ``None``."""
    return None if value is None else space_key(value, field=field)


def require_current_page(metadata: PageMetadata, *, allow_missing_status: bool = False) -> None:
    """Require page metadata returned by Confluence to describe a current page."""
    if metadata.content_type != "page":
        raise InvalidRequest("This tool accepts Confluence pages only.")
    if metadata.status != "current" and not (allow_missing_status and metadata.status is None):
        raise InvalidRequest("This tool accepts current published pages only.")
