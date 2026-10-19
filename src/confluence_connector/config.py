"""Shared stdlib-only loader for private app and research configuration."""

from __future__ import annotations

import json
from argparse import ArgumentParser
from functools import lru_cache
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

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
    required = {"base_url", "query", "form_auth_path", "endpoints"}
    if not isinstance(settings, dict) or set(settings) != required:
        raise SystemExit(
            "Configuration must contain exactly base_url, query, form_auth_path and endpoints."
        )
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
    return settings


def data_dir() -> Path:
    """Return the per-user application directory; the named environment setting overrides it."""
    override = os.environ.get("CONFLUENCE_CONNECTOR_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        return Path.home() / f".{APP_NAME}"
    root = os.environ.get("XDG_DATA_HOME")
    return (Path(root) if root else Path.home() / ".local" / "share") / APP_NAME
