"""Resolve caller page IDs and Confluence page URLs without trusting foreign hosts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from confluence_connector.domain.errors import InvalidRequest, Upstream
from confluence_connector.domain.models import ResolvedPageReference
from confluence_connector.domain.urls import same_instance_origin
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.transport import ConfluenceTransport

MAX_SHORT_LINK_REDIRECTS = 5
_PAGE_PATH = re.compile(r"^/spaces/[^/]+/pages/(\d+)(?:/.*)?$")


class PageLinkResolver:
    """Resolve the documented page URL forms; callers validate references at their public boundary."""

    def __init__(
        self,
        settings: Mapping[str, Any],
        transport: ConfluenceTransport,
        api: ConfluenceApi,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._api = api
        base = urlsplit(str(settings["base_url"]))
        self._base_url = str(settings["base_url"])
        self._base_path = base.path.rstrip("/")

    async def resolve(self, reference: str, *, expected_username: str, status: str | None = None) -> ResolvedPageReference:
        """Resolve a validated reference; internal callers may select the trashed title lookup."""
        value = reference.strip()
        if value.isdecimal():
            return ResolvedPageReference(page_id=value)
        parsed = self._parse_instance_url(value)
        return await self._resolve_path(parsed.path, parsed.query, expected_username=expected_username, status=status)

    async def _resolve_path(
        self, path: str, query: str, *, expected_username: str, status: str | None = None, visited: frozenset[str] = frozenset()
    ) -> ResolvedPageReference:
        site_path = self._site_path(path)
        query_values = parse_qs(query, keep_blank_values=False)
        version = _version(query_values)
        if site_path.endswith(("/viewpage.action", "/editpage.action")):
            page_id = _first(query_values, "pageId")
            if not page_id or not page_id.isdecimal():
                raise InvalidRequest("A viewpage or editpage URL must include a numeric pageId.")
            return ResolvedPageReference(page_id=page_id, version=version)
        page_path = _PAGE_PATH.fullmatch(site_path)
        if page_path:
            return ResolvedPageReference(page_id=page_path.group(1), version=version)
        segments = [part for part in site_path.split("/") if part]
        if segments and segments[0] == "display" and len(segments) >= 3:
            space_key = unquote(segments[1])
            title = unquote("/".join(segments[2:]).replace("+", " "))
            page_id = await self._api.find_page_by_title(space_key, title, expected_username=expected_username, status=status)
            return ResolvedPageReference(page_id=page_id, version=version)
        if len(segments) == 2 and segments[0] == "x":
            marker = "/".join((site_path, query))
            if marker in visited:
                raise Upstream("Confluence returned a short-link redirect loop.")
            if len(visited) >= MAX_SHORT_LINK_REDIRECTS:
                raise Upstream("Confluence returned too many short-link redirects.")
            location = await self._transport.redirect_location(
                "/".join(segments),
                expected_username=expected_username,
                params={"pageVersion": str(version)} if version is not None else None,
            )
            if not location:
                raise Upstream("Confluence did not resolve the selected short link.")
            target = self._parse_instance_url(location)
            resolved = await self._resolve_path(
                target.path,
                target.query,
                expected_username=expected_username,
                visited=visited | {marker},
                status=status,
            )
            return resolved.model_copy(update={"version": version or resolved.version})
        raise InvalidRequest("Use a page ID or a supported Confluence page URL.")

    def _parse_instance_url(self, value: str):
        try:
            parsed = urlsplit(value)
        except ValueError:
            raise InvalidRequest("The page URL has an invalid host or port.") from None
        if not parsed.scheme or not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise InvalidRequest("Page URLs must be absolute URLs for the configured Confluence instance.")
        try:
            parsed.port
        except ValueError:
            raise InvalidRequest("The page URL has an invalid host or port.") from None
        if not same_instance_origin(self._base_url, value):
            raise InvalidRequest("Page URLs on hosts other than the configured Confluence instance are refused.")
        self._site_path(parsed.path)
        return parsed

    def _site_path(self, path: str) -> str:
        if self._base_path:
            if path == self._base_path:
                return "/"
            if not path.startswith(self._base_path + "/"):
                raise InvalidRequest("The page URL is outside the configured Confluence context path.")
            return path[len(self._base_path) :]
        return path


def _version(query: Mapping[str, list[str]]) -> int | None:
    value = _first(query, "pageVersion")
    if value is None:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise InvalidRequest("pageVersion must be a positive integer.")
    return int(value)


def _first(values: Mapping[str, list[str]], key: str) -> str | None:
    matches = values.get(key)
    return matches[0] if matches else None
