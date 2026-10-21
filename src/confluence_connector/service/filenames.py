"""Shared cross-platform filename cleanup for downloaded attachments and exports."""

from __future__ import annotations

import re

MAX_FILENAME_COMPONENT_UNITS = 240
_BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAME = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³]) *(?:\..*)?$", re.IGNORECASE
)


def filename_component_units(value: str) -> int:
    """Return the stricter of encoded-byte and UTF-16 filename component lengths."""
    return max(
        len(value.encode("utf-8", errors="surrogatepass")),
        len(value.encode("utf-16-le", errors="surrogatepass")) // 2,
    )


def sanitize_filename_component(value: str) -> str:
    """Replace invalid filesystem characters and remove trailing spaces and periods."""
    return _BAD_FILENAME.sub("_", value).strip().rstrip(" .")


def safe_filename_stem(
    value: str,
    *,
    suffix: str,
    maximum_length: int,
    maximum_component_units: int = MAX_FILENAME_COMPONENT_UNITS,
    fallback: str,
) -> str:
    """Sanitize a filename stem while reserving room for its identity or extension suffix."""
    stem = sanitize_filename_component(value) or fallback
    if _RESERVED_WINDOWS_NAME.fullmatch(stem) or _RESERVED_WINDOWS_NAME.fullmatch(stem + suffix):
        stem = f"_{stem}"
    max_stem_length = max(1, maximum_length - len(suffix))
    stem = stem[:max_stem_length].rstrip(" .")
    while stem and (
        len(stem + suffix) > maximum_length
        or filename_component_units(stem + suffix) > maximum_component_units
    ):
        stem = stem[:-1].rstrip(" .")
    if stem:
        return stem

    fallback_stem = sanitize_filename_component(fallback) or "_"
    fallback_stem = fallback_stem[:max_stem_length].rstrip(" .")
    while fallback_stem and (
        len(fallback_stem + suffix) > maximum_length
        or filename_component_units(fallback_stem + suffix) > maximum_component_units
    ):
        fallback_stem = fallback_stem[:-1].rstrip(" .")
    return fallback_stem or "_"
