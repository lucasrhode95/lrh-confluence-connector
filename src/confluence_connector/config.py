"""Private configuration loading and atomic CQL-timezone calibration updates."""

from __future__ import annotations

import json
from argparse import ArgumentParser
from functools import lru_cache
import os
import re
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from msal_extensions import CrossPlatLock

from confluence_connector.timezones import parse_cql_timezone

APP_NAME = "lrh-confluence-connector"
CLI_NAME = "confluence-connector"
MCP_SERVER_NAME = "lrh-confluence-connector"
LOG_LEVEL_VARIABLE = "CONFLUENCE_CONNECTOR_LOG_LEVEL"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "confluence_config.json"
CONFIG_PATH = Path(os.environ.get("CONFLUENCE_CONFIG", str(DEFAULT_CONFIG_PATH)))
_selected_path = CONFIG_PATH.resolve()


def load_config(path: Path | None = None) -> dict:
    """Entry point: select the optional command override; all callers share its cached settings."""
    global _selected_path
    if path is not None:
        _selected_path = path.resolve()
    return _read_config(_selected_path)


def add_config_argument(parser: ArgumentParser) -> None:
    """Add the same optional configuration override to every command."""
    parser.add_argument(
        "--config",
        type=Path,
        help="Override CONFLUENCE_CONFIG or the repository's confluence_config.json.",
    )


@lru_cache(maxsize=1)
def _read_config(path: Path) -> dict:
    """Load and validate JSON once for the selected file; no embedded setting defaults."""
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(
            "Missing Confluence configuration. Copy confluence_config.example.json to confluence_config.json."
        ) from None
    except (OSError, json.JSONDecodeError):
        raise SystemExit("Cannot parse the local configuration as valid JSON.") from None
    _validate_settings(settings)
    return settings


def write_cql_timezone_if_missing(value: str, path: Path | None = None) -> bool:
    """Atomically record a measured timezone only when the private setting is currently absent.

    The selected config is re-read while a cross-process lock is held, so concurrent CLI processes
    preserve existing settings and never replace an explicit timezone. Internal callers pass a
    calibrated, formatted fixed-offset value.
    """
    try:
        parse_cql_timezone(value)
    except ValueError:
        raise SystemExit("The calibrated cql_timezone value is invalid.") from None
    target = (path or _selected_path).resolve()
    lock_path = data_dir() / "config-write.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with CrossPlatLock(str(lock_path)):
            try:
                settings = json.loads(target.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                raise SystemExit("Cannot update the local configuration safely.") from None
            _validate_settings(settings)
            if settings.get("cql_timezone") is not None:
                _read_config.cache_clear()
                return False
            settings["cql_timezone"] = value
            _atomic_write_json(target, settings)
    except OSError:
        raise SystemExit("Cannot update the local configuration atomically.") from None
    _read_config.cache_clear()
    return True


def _validate_settings(settings: object) -> None:
    """Validate configuration once at the file boundary before callers trust its values."""
    required = {"base_url", "query", "form_auth_path", "endpoints"}
    if not isinstance(settings, dict) or set(settings) not in (required, required | {"cql_timezone"}):
        raise SystemExit(
            "Configuration must contain base_url, query, form_auth_path, endpoints and optional cql_timezone."
        )
    cql_timezone = settings.get("cql_timezone")
    if cql_timezone is not None:
        try:
            parse_cql_timezone(cql_timezone)
        except ValueError:
            raise SystemExit("cql_timezone must be a valid IANA name or UTC±HH:MM fixed offset.") from None
    url = settings["base_url"]
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (
        not parsed
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("base_url must be an HTTPS URL without credentials, query or fragment.")
    try:
        parsed.port
    except ValueError:
        raise SystemExit("base_url must contain a valid HTTPS port.") from None
    if not isinstance(settings["query"], str) or not settings["query"].strip():
        raise SystemExit("query must contain a nonempty synthetic search term.")
    auth_path = settings["form_auth_path"]
    if (
        not isinstance(auth_path, str)
        or not auth_path
        or auth_path.startswith("/")
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", auth_path)
        or ".." in auth_path.split("/")
    ):
        raise SystemExit("form_auth_path must be a relative API path without query or traversal.")
    endpoints = settings["endpoints"]
    if not isinstance(endpoints, dict) or set(endpoints) != {
        "current_user",
        "search_pages",
        "ui_search",
        "spaces",
        "content",
    }:
        raise SystemExit(
            "endpoints must define current_user, search_pages, ui_search, spaces and content."
        )
    for key, endpoint in endpoints.items():
        if (
            not isinstance(endpoint, str)
            or not endpoint
            or endpoint.startswith("/")
            or ".." in endpoint.split("/")
        ):
            raise SystemExit("Endpoints must be relative paths without traversal.")
        route = endpoint.replace("{page_id}", "123") if key == "content" else endpoint
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", route) or (
            key == "content" and endpoint.count("{page_id}") != 1
        ):
            raise SystemExit(
                "Only content may contain one {page_id} placeholder; endpoint queries are not allowed."
            )


def _atomic_write_json(path: Path, settings: dict) -> None:
    """Replace one JSON config from a same-directory flushed temporary file."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(settings, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def data_dir() -> Path:
    """Return the per-user application directory; the named environment setting overrides it."""
    override = os.environ.get("CONFLUENCE_CONNECTOR_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path.home() / f".{APP_NAME}"
    root = os.environ.get("XDG_DATA_HOME")
    return (Path(root) if root else Path.home() / ".local" / "share") / APP_NAME
