"""Same-instance URL checks shared by source mapping and remote route handling."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit


def same_instance_origin(base_url: str, value: Any) -> bool:
    """Compare an absolute URL's scheme, host and effective port with the configured instance."""
    if not isinstance(value, str) or not value:
        return False
    try:
        base = urlsplit(base_url)
        link = urlsplit(value)
        return (
            link.scheme.casefold() == base.scheme.casefold()
            and (link.hostname or "").casefold() == (base.hostname or "").casefold()
            and _port(link) == _port(base)
            and link.username is None
            and link.password is None
        )
    except ValueError:
        return False


def same_instance_link(base_url: str | None, value: Any) -> str | None:
    """Normalize a relative link or accept an absolute one only inside the instance context."""
    if not isinstance(value, str) or not value:
        return None
    try:
        link = urlsplit(value)
    except ValueError:
        return None
    if base_url is None:
        return value if not link.scheme and not link.netloc else None

    try:
        base = urlsplit(base_url)
    except ValueError:
        return None
    context = base.path.rstrip("/")
    if not link.scheme and not link.netloc and link.path.startswith("/") and context:
        if link.path != context and not link.path.startswith(context + "/"):
            value = urlunsplit(("", "", context + link.path, link.query, link.fragment))
    target = value if link.scheme or link.netloc else urljoin(base_url.rstrip("/") + "/", value)
    try:
        target_parts = urlsplit(target)
    except ValueError:
        return None
    if not same_instance_origin(base_url, target):
        return None
    in_context = not context or target_parts.path == context or target_parts.path.startswith(context + "/")
    return target if in_context else None


def _port(parts: Any) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme.casefold() == "https" else 80
