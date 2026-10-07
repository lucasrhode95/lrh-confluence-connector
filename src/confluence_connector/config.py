"""Shared stdlib-only loader for private app and research configuration."""

from __future__ import annotations

import json
from argparse import ArgumentParser
from functools import lru_cache
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

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


def capture_inputs(settings: dict, folder: Path) -> list[tuple[Path, str]]:
    """Entry point: require existing captures selected by the already validated settings."""
    if not settings["captures"]:
        raise SystemExit("Configure at least one capture for offline research.")
    inputs = [(folder / entry["file"], entry["label"]) for entry in settings["captures"]]
    if any(not path.is_file() for path, label in inputs):
        raise SystemExit("One or more configured capture files are missing.")
    return inputs


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
    required = {"base_url", "query", "form_auth_path", "captures", "endpoints"}
    if not isinstance(settings, dict) or set(settings) != required:
        raise SystemExit(
            "Configuration must contain exactly base_url, query, form_auth_path, captures and endpoints."
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
    if not isinstance(settings["captures"], list):
        raise SystemExit(
            "captures must be a list; use an empty list when only probing live authentication."
        )
    labels = set()
    files = set()
    for capture in settings["captures"]:
        if not isinstance(capture, dict) or set(capture) != {"file", "label"}:
            raise SystemExit("Each capture must contain exactly file and label.")
        filename, label = capture["file"], capture["label"]
        if (
            not isinstance(filename, str)
            or not filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise SystemExit("Capture file must be a basename inside captures-dir.")
        if not isinstance(label, str) or not re.fullmatch(r"HAR[0-9]+|ZIP[0-9]*", label):
            raise SystemExit("Capture labels must be neutral aliases such as HAR01 or ZIP.")
        suffix = Path(filename).suffix.lower()
        if suffix != (".har" if label.startswith("HAR") else ".zip"):
            raise SystemExit("Capture label and file type must agree.")
        if label in labels or filename in files:
            raise SystemExit("Capture filenames and labels must be unique.")
        labels.add(label)
        files.add(filename)
    return settings
