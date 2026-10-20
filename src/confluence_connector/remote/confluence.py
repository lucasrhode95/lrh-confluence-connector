"""Documented Confluence REST routes and mapping into domain models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from confluence_connector.domain.errors import InvalidRequest, NotFound, Upstream
from confluence_connector.domain.models import CurrentUser, SpaceList, SpaceSummary
from confluence_connector.remote.transport import ConfluenceTransport

SPACE_PAGE_SIZE = 500


class ConfluenceApi:
    """Own configured REST routes and map source JSON before returning typed values."""

    def __init__(self, settings: Mapping[str, Any], transport: ConfluenceTransport) -> None:
        self._settings = settings
        self._transport = transport

    async def current_user(
        self, *, token_override: str | None = None, expected_username: str | None = None
    ) -> CurrentUser:
        """Read current-user identity from the configured endpoint without logging the response."""
        payload = await self._transport.get_json(
            self._endpoint("current_user"),
            token_override=token_override,
            expected_username=expected_username,
        )
        username = payload.get("username") or payload.get("userKey")
        if not isinstance(username, str) or not username.strip():
            raise Upstream("Confluence returned no stable current-user identifier.")
        return CurrentUser(username=username.strip())

    async def list_spaces(self, *, expected_username: str) -> SpaceList:
        """Read current and archived visible spaces using server ``start + size`` paging."""
        endpoint = self._endpoint("spaces")
        results: list[SpaceSummary] = []
        seen_keys: set[str] = set()
        statuses = ("current", "archived")
        reported_totals: dict[str, int | None] = dict.fromkeys(statuses)
        returned_counts = dict.fromkeys(statuses, 0)
        for status in statuses:
            start = 0
            while True:
                payload = await self._transport.get_json(
                    endpoint,
                    params={"limit": str(SPACE_PAGE_SIZE), "start": str(start), "status": status},
                    expected_username=expected_username,
                )
                raw_items = payload.get("results")
                if not isinstance(raw_items, list):
                    raise Upstream("Confluence returned an unexpected spaces response.")
                page_start = _nonnegative_int(payload.get("start"), default=start)
                if page_start != start:
                    raise Upstream("Confluence returned an inconsistent spaces page offset.")
                page_size = _nonnegative_int(payload.get("size"), default=len(raw_items))
                if page_size != len(raw_items):
                    raise Upstream("Confluence returned an inconsistent spaces page size.")
                total = payload.get("totalSize", payload.get("totalCount"))
                if total is not None and (
                    not isinstance(total, int) or isinstance(total, bool) or total < 0
                ):
                    raise Upstream("Confluence returned an invalid total space count.")
                if isinstance(total, int):
                    reported_totals[status] = total
                page = [self._space_summary(item) for item in raw_items]
                for space in page:
                    if space.key in seen_keys:
                        raise Upstream("Confluence repeated a space while paging the result.")
                    seen_keys.add(space.key)
                results.extend(page)
                returned_counts[status] += page_size
                if page_size == 0:
                    break
                next_start = page_start + page_size
                reported_total = reported_totals[status]
                if reported_total is not None and next_start >= reported_total:
                    break
                if page_size < SPACE_PAGE_SIZE and reported_total is None:
                    break
                if next_start <= start:
                    raise Upstream("Confluence did not advance the spaces page offset.")
                start = next_start
        total_count = sum(
            reported if reported is not None else returned_counts[status]
            for status, reported in reported_totals.items()
        )
        total_count = max(total_count, len(results))
        return SpaceList(spaces=results, total_count=total_count, truncated=len(results) < total_count)

    async def find_page_by_title(
        self, space_key: str, title: str, *, expected_username: str
    ) -> str:
        """Resolve a legacy display URL by its validated space key and decoded title."""
        if not space_key or not title:
            raise InvalidRequest("A display URL must include both a space key and page title.")
        cql = (
            f"type = page AND space = {_cql_literal(space_key)} "
            f"AND title = {_cql_literal(title)}"
        )
        payload = await self._transport.get_json(
            self._endpoint("search_pages"),
            params={"cql": cql, "limit": "2", "expand": "space"},
            expected_username=expected_username,
        )
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise Upstream("Confluence returned an unexpected page-title search response.")
        matches = [row for row in rows if isinstance(row, dict) and _page_id(row.get("id"))]
        if not matches:
            raise NotFound("The page in this display URL no longer matches a page; it may have been renamed.")
        if len(matches) > 1:
            raise Upstream("The display URL matched more than one page in its space.")
        return str(matches[0]["id"])

    def _endpoint(self, name: str) -> str:
        endpoints = self._settings["endpoints"]
        return str(endpoints[name])

    def _space_summary(self, item: Any) -> SpaceSummary:
        if not isinstance(item, dict):
            raise Upstream("Confluence returned a malformed space entry.")
        key = item.get("key")
        name = item.get("name")
        if not isinstance(key, str) or not key or not isinstance(name, str) or not name:
            raise Upstream("Confluence returned a space without its key or name.")
        item_id = item.get("id")
        if isinstance(item_id, int) and not isinstance(item_id, bool):
            item_id = str(item_id)
        elif not isinstance(item_id, str):
            item_id = None
        kind = item.get("type") if isinstance(item.get("type"), str) else None
        status = item.get("status") if isinstance(item.get("status"), str) else None
        links = item.get("_links")
        webui = links.get("webui") if isinstance(links, dict) else None
        return SpaceSummary(
            id=item_id,
            key=key,
            name=name,
            type=kind,
            status=status,
            url=_same_instance_link(str(self._settings["base_url"]), webui),
        )


def _nonnegative_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise Upstream("Confluence returned an invalid spaces paging value.")


def _page_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdigit()


def _cql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _same_instance_link(base_url: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    if value.startswith("/") and base_path and not value.startswith(base_path + "/"):
        target = urlunsplit((base.scheme, base.netloc, base_path + value, "", ""))
    else:
        target = urljoin(base_url.rstrip("/") + "/", value)
    link = urlsplit(target)
    try:
        same_origin = (
            base.scheme.casefold() == link.scheme.casefold()
            and (base.hostname or "").casefold() == (link.hostname or "").casefold()
            and _port(base) == _port(link)
        )
    except ValueError:
        return None
    in_context = not base_path or link.path == base_path or link.path.startswith(base_path + "/")
    return target if same_origin and in_context and link.username is None and link.password is None else None


def _port(parts: Any) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme == "https" else 80
