"""Write and read storage-body files: the rich export header and `body_file` inputs.

`export_pages` writes each rich file as a header comment followed by the unchanged storage body, and
optionally a comments section. `create_page` and `update_page` read such files back. The header is
the connector's own provenance record, so its `Page ID:` and `Version:` lines are the source of the
file's page identity; nothing else in the file is interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from confluence_connector.domain.errors import InvalidRequest

COMMENTS_SECTION_CLASS = "confluence-comments"
_HEADER = re.compile(r"\A﻿?<!--\r?\n(?P<header>.*?)\r?\n-->\r?\n", re.DOTALL)
_PAGE_ID_LINE = re.compile(r"^Page ID: (\d+)$")
_VERSION_LINE = re.compile(r"^Version: (\d+)(?: of \d+)?$")


@dataclass(frozen=True)
class ExportHeader:
    """Page identity recorded by a rich export file's leading header comment."""

    page_id: str
    version: int


def rich_export_content(header: str, page_id: str, body: str) -> str:
    """Return a rich export file: a provenance comment with the page ID, then the storage body."""
    metadata = f"{header}\nPage ID: {page_id}".replace("--", "—")
    return f"<!--\n{metadata}\n-->\n{body}"


def read_storage_file(path: Any) -> str:
    """Entry point: read one absolute UTF-8 `body_file` path, without a byte-order mark."""
    if not isinstance(path, str) or not path.strip():
        raise InvalidRequest("body_file must be an absolute file path.")
    file = Path(path)
    if not file.is_absolute():
        raise InvalidRequest("body_file must be an absolute file path.")
    if not file.is_file():
        raise InvalidRequest("body_file does not name a readable file.")
    try:
        text = file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise InvalidRequest("body_file must be UTF-8 text.") from None
    except OSError as exc:
        raise InvalidRequest(f"body_file could not be read ({type(exc).__name__}).") from None
    return text.removeprefix("﻿")


def split_export_header(text: str) -> tuple[ExportHeader | None, str]:
    """Separate a rich export's header from its storage body; plain storage has no header.

    A header comment without exactly one page ID and one version line is refused, because the file
    would otherwise be applied to a page the caller did not export.
    """
    match = _HEADER.match(text)
    if match is None:
        _refuse_comments_section(text)
        return None, text
    page_ids: list[str] = []
    versions: list[int] = []
    for line in match.group("header").splitlines():
        if page_id := _PAGE_ID_LINE.match(line):
            page_ids.append(page_id.group(1))
        elif version := _VERSION_LINE.match(line):
            versions.append(int(version.group(1)))
    if len(page_ids) != 1 or len(versions) != 1:
        raise InvalidRequest(
            "body_file's header comment has no single Page ID and Version line; export the page again."
        )
    body = text[match.end():]
    _refuse_comments_section(body)
    return ExportHeader(page_id=page_ids[0], version=versions[0]), body


def _refuse_comments_section(body: str) -> None:
    soup = BeautifulSoup(body, "html.parser")
    if soup.find("section", class_=COMMENTS_SECTION_CLASS) is not None:
        raise InvalidRequest(
            "body_file contains the exported comments section; export with parse_scope.comments false."
        )
