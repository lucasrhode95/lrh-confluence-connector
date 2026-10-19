"""Documented Confluence REST routes and mapping into domain models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from confluence_connector.domain.errors import InvalidRequest, NotFound, Upstream
from confluence_connector.domain.models import (
    ContentSearchHit,
    CurrentUser,
    PageMetadata,
    PageSummary,
    PageVersion,
    PersonSummary,
    RemoteContentSearchPage,
    RemoteUserSearchPage,
    SpaceList,
    SpaceSummary,
)
from confluence_connector.remote.transport import ConfluenceTransport

SPACE_PAGE_SIZE = 500
CHILD_PAGE_SIZE = 500
VERSION_PAGE_SIZE = 200
MAX_PAGE_VERSIONS = 5_000


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

    async def search_content_page(
        self,
        cql: str,
        *,
        start: int,
        limit: int,
        include_archived: bool,
        expected_username: str,
    ) -> RemoteContentSearchPage:
        """Read one CQL page and map the source result fields before returning typed hits."""
        payload = await self._transport.get_json(
            self._endpoint("ui_search"),
            params={
                "cql": cql,
                "start": str(start),
                "limit": str(limit),
                "includeArchivedSpaces": str(include_archived).lower(),
                "expand": "content.space,content.version,content.ancestors",
            },
            expected_username=expected_username,
        )
        return _content_search_page(
            payload, requested_start=start, base_url=str(self._settings["base_url"])
        )

    async def quicknav_title_matches(
        self, query: str, *, limit: int, expected_username: str
    ) -> list[ContentSearchHit]:
        """Read the browser-observed quick-navigation title source without trusting returned URLs."""
        payload = await self._transport.get_json(
            "rest/quicknav/1/search",
            params={"query": query, "limit": str(limit), "src": "next.ui.search", "type": "page"},
            expected_username=expected_username,
        )
        values = payload.get("contentNameMatches")
        if not isinstance(values, list):
            raise Upstream("Confluence returned an unexpected quick-navigation response.")
        matches: list[ContentSearchHit] = []
        for value in values:
            hit = _quicknav_hit(value, base_url=str(self._settings["base_url"]))
            if hit is not None:
                matches.append(hit)
        return matches

    async def search_users_page(
        self, name: str, *, start: int, limit: int, expected_username: str
    ) -> RemoteUserSearchPage:
        """Resolve a current user's display name through the observed user CQL search."""
        cql = f"type = user AND user.fullname ~ {_cql_literal(name)}"
        payload = await self._transport.get_json(
            self._endpoint("ui_search"),
            params={"cql": cql, "start": str(start), "limit": str(limit)},
            expected_username=expected_username,
        )
        page = _content_search_page(
            payload, requested_start=start, base_url=str(self._settings["base_url"])
        )
        users: list[PersonSummary] = []
        for row in _result_rows(payload):
            user = _person(_first_dict(row, "user", "entity"))
            if user is None:
                user = PersonSummary(
                    username=_optional_string(row.get("username")),
                    user_key=_optional_string(row.get("userKey")),
                    display_name=_optional_string(row.get("title")),
                    user_type=_optional_string(row.get("type")),
                )
            if user.username or user.user_key or user.display_name:
                users.append(user)
        return RemoteUserSearchPage(
            users=users,
            start=page.start,
            size=page.size,
            total_size=page.total_size,
        )

    async def get_user(self, username: str, *, expected_username: str) -> PersonSummary:
        """Read a user by exact key, including former users absent from name search."""
        payload = await self._transport.get_json(
            "rest/api/user",
            params={"key": username},
            expected_username=expected_username,
        )
        user = _person(payload)
        if user is None or not (user.username or user.user_key):
            raise Upstream("Confluence returned a user without a username or user key.")
        return user

    async def get_page_metadata(self, page_id: str, *, expected_username: str) -> PageMetadata:
        """Read one page's expanded history, ancestors and labels; ``page_id`` is service-validated."""
        payload = await self._transport.get_json(
            self._content_route(page_id),
            params={"expand": "space,version,history,ancestors,metadata.labels,history.contributors"},
            expected_username=expected_username,
        )
        return _page_metadata(payload, base_url=str(self._settings["base_url"]))

    async def list_page_versions(
        self, page_id: str, *, expected_username: str
    ) -> tuple[list[PageVersion], bool]:
        """Read the experimental version list by actual ``start + size`` until it ends or reaches the cap."""
        versions: list[PageVersion] = []
        start = 0
        while len(versions) < MAX_PAGE_VERSIONS:
            payload = await self._transport.get_json(
                f"rest/experimental/content/{page_id}/version",
                params={"start": str(start), "limit": str(VERSION_PAGE_SIZE)},
                expected_username=expected_username,
            )
            rows = payload.get("results")
            if not isinstance(rows, list):
                raise Upstream("Confluence returned an unexpected page-version response.")
            actual_start = _nonnegative_int(payload.get("start"), default=start)
            if actual_start != start:
                raise Upstream("Confluence returned an inconsistent page-version offset.")
            actual_size = _nonnegative_int(payload.get("size"), default=len(rows))
            if actual_size != len(rows):
                raise Upstream("Confluence returned an inconsistent page-version size.")
            versions.extend(_page_version(row) for row in rows)
            if actual_size == 0:
                return versions, False
            next_start = actual_start + actual_size
            total = _optional_nonnegative_int(payload.get("totalSize", payload.get("totalCount")))
            has_next = _has_next_link(payload)
            if total is not None and next_start >= total:
                return versions, False
            if total is None and not has_next:
                return versions, False
            if next_start <= start:
                raise Upstream("Confluence did not advance the page-version offset.")
            start = next_start
        return versions[:MAX_PAGE_VERSIONS], True

    async def list_child_pages(
        self, page_id: str, *, start: int, expected_username: str
    ) -> RemoteContentSearchPage:
        """Read one ordered direct-child page; the page id and offset are service-validated."""
        payload = await self._transport.get_json(
            f"{self._content_route(page_id)}/child/page",
            params={"start": str(start), "limit": str(CHILD_PAGE_SIZE)},
            expected_username=expected_username,
        )
        return _content_search_page(
            payload, requested_start=start, base_url=str(self._settings["base_url"])
        )

    async def list_space_root_pages(
        self, space_key: str, *, start: int, expected_username: str
    ) -> RemoteContentSearchPage:
        """Read one ordered page of top-level space pages; the service validates the key and offset."""
        route = f"rest/api/space/{quote(space_key, safe='')}/content/page"
        payload = await self._transport.get_json(
            route,
            params={"start": str(start), "limit": str(CHILD_PAGE_SIZE)},
            expected_username=expected_username,
        )
        return _content_search_page(
            payload, requested_start=start, base_url=str(self._settings["base_url"])
        )

    def _endpoint(self, name: str) -> str:
        endpoints = self._settings["endpoints"]
        return str(endpoints[name])

    def _content_route(self, page_id: str) -> str:
        return self._endpoint("content").replace("{page_id}", page_id)

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


def _content_search_page(
    payload: dict[str, Any], *, requested_start: int, base_url: str
) -> RemoteContentSearchPage:
    """Validate the server page envelope and map source rows before service-level processing."""
    rows = _result_rows(payload)
    start = _nonnegative_int(payload.get("start"), default=requested_start)
    if start != requested_start:
        raise Upstream("Confluence returned an inconsistent content-search offset.")
    size = _nonnegative_int(payload.get("size"), default=len(rows))
    if size != len(rows):
        raise Upstream("Confluence returned an inconsistent content-search size.")
    total_size = _optional_nonnegative_int(payload.get("totalSize", payload.get("totalCount")))
    return RemoteContentSearchPage(
        hits=[_content_search_hit(row, base_url=base_url) for row in rows],
        start=start,
        size=size,
        total_size=total_size,
    )


def _result_rows(payload: dict[str, Any]) -> list[Any]:
    """Return an API results array, failing explicitly when the response envelope is malformed."""
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise Upstream("Confluence returned an unexpected search response.")
    return rows


def _content_search_hit(value: Any, *, base_url: str) -> ContentSearchHit:
    """Map a search or child-content source row without inferring fields from rendered text."""
    if not isinstance(value, dict):
        raise Upstream("Confluence returned a malformed content-search result.")
    content = _first_dict(value, "content", "entity") or value
    content_id = _identifier(content.get("id", value.get("id", value.get("contentId"))))
    title = _optional_string(value.get("title")) or _optional_string(content.get("title"))
    if title is None:
        raise Upstream("Confluence returned a content-search result without a title.")
    content_type = (
        _optional_string(content.get("type"))
        or _optional_string(value.get("entityType"))
        or _optional_string(value.get("type"))
    )
    resource_type = value.get("resourceType")
    if content_type is None and isinstance(resource_type, dict):
        content_type = _optional_string(resource_type.get("name"))
    space = content.get("space")
    if not isinstance(space, dict):
        space = value.get("space") if isinstance(value.get("space"), dict) else {}
    ancestors = _page_summaries(content.get("ancestors"), base_url=base_url)
    version = content.get("version")
    version = version if isinstance(version, dict) else {}
    links = content.get("_links")
    links = links if isinstance(links, dict) else {}
    extensions = content.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    position = _optional_nonnegative_int(content.get("position", extensions.get("position")))
    raw_url = _optional_string(value.get("url")) or _optional_string(links.get("webui"))
    return ContentSearchHit(
        content_id=content_id,
        content_type=content_type,
        title=title,
        space_key=_optional_string(space.get("key")),
        space_name=_optional_string(space.get("name")),
        url=_same_instance_link(base_url, raw_url),
        excerpt=_optional_string(value.get("excerpt")),
        modified_at=_optional_string(version.get("when")) or _optional_string(value.get("lastModified")),
        parent_id=ancestors[-1].page_id if ancestors else None,
        ancestors=ancestors,
        position=position,
        sources=["cql"],
    )


def _quicknav_hit(value: Any, *, base_url: str) -> ContentSearchHit | None:
    """Map only quick-navigation rows that provide a usable source id and title."""
    if not isinstance(value, dict):
        return None
    content_id = _identifier(value.get("contentId", value.get("id")))
    if content_id is None or not content_id.isdecimal():
        return None
    title = _optional_string(value.get("title")) or _optional_string(value.get("name"))
    if title is None:
        return None
    space = value.get("space")
    space = space if isinstance(space, dict) else {}
    raw_url = _optional_string(value.get("url")) or _optional_string(value.get("href"))
    return ContentSearchHit(
        content_id=content_id,
        content_type=(
            _optional_string(value.get("type"))
            or _optional_string(value.get("entityType"))
            or "page"
        ),
        title=title,
        space_key=_optional_string(value.get("spaceKey")) or _optional_string(space.get("key")),
        space_name=_optional_string(value.get("spaceName")) or _optional_string(space.get("name")),
        url=_same_instance_link(base_url, raw_url),
        modified_at=_optional_string(value.get("lastModified")),
        sources=["quicknav"],
    )


def _page_metadata(payload: dict[str, Any], *, base_url: str) -> PageMetadata:
    """Map a content response into metadata from source fields and ordered ancestor records."""
    page_id = _identifier(payload.get("id"))
    title = _optional_string(payload.get("title"))
    content_type = _optional_string(payload.get("type"))
    if page_id is None or title is None or content_type is None:
        raise Upstream("Confluence returned incomplete page metadata.")
    space = payload.get("space")
    space = space if isinstance(space, dict) else {}
    history = payload.get("history")
    history = history if isinstance(history, dict) else {}
    version = payload.get("version")
    version = version if isinstance(version, dict) else {}
    links = payload.get("_links")
    links = links if isinstance(links, dict) else {}
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return PageMetadata(
        page_id=page_id,
        title=title,
        content_type=content_type,
        status=_optional_string(payload.get("status")),
        url=_same_instance_link(base_url, links.get("webui")),
        space_key=_optional_string(space.get("key")),
        space_name=_optional_string(space.get("name")),
        created_at=_optional_string(history.get("createdDate"))
        or _optional_string(payload.get("creationDate")),
        created_by=_person(history.get("createdBy")),
        modified_at=_optional_string(version.get("when"))
        or _optional_string(payload.get("lastModificationDate")),
        modified_by=_person(version.get("by")) or _person(payload.get("lastModifier")),
        version=_optional_nonnegative_int(version.get("number")),
        ancestors=_page_summaries(payload.get("ancestors"), base_url=base_url),
        labels=_labels(metadata.get("labels")),
        contributors=_people(history.get("contributors")),
    )


def _page_summaries(value: Any, *, base_url: str) -> list[PageSummary]:
    """Map ordered source ancestor records; malformed optional rows are omitted, never reparsed from text."""
    if not isinstance(value, list):
        return []
    summaries: list[PageSummary] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        page_id = _identifier(item.get("id"))
        title = _optional_string(item.get("title"))
        if page_id is None or title is None:
            continue
        space = item.get("space")
        space = space if isinstance(space, dict) else {}
        links = item.get("_links")
        links = links if isinstance(links, dict) else {}
        summaries.append(
            PageSummary(
                page_id=page_id,
                title=title,
                space_key=_optional_string(space.get("key")),
                url=_same_instance_link(base_url, links.get("webui")),
                position=_optional_nonnegative_int(item.get("position")),
            )
        )
    return summaries


def _labels(value: Any) -> list[str]:
    """Read label names from either the list or paged label envelope."""
    if isinstance(value, dict):
        value = value.get("results")
    if not isinstance(value, list):
        return []
    labels = []
    for item in value:
        name = item if isinstance(item, str) else item.get("name") if isinstance(item, dict) else None
        if isinstance(name, str) and name.strip():
            labels.append(name.strip())
    return labels


def _people(value: Any) -> list[PersonSummary]:
    """Map source contributor containers without inferring user identity from display names."""
    if isinstance(value, dict):
        value = value.get("results", value.get("users"))
    if not isinstance(value, list):
        return []
    return [person for item in value if (person := _person(item)) is not None]


def _person(value: Any) -> PersonSummary | None:
    if not isinstance(value, dict):
        return None
    person = PersonSummary(
        username=_optional_string(value.get("username")),
        user_key=_optional_string(value.get("userKey")) or _optional_string(value.get("user_key")),
        display_name=_optional_string(value.get("displayName")) or _optional_string(value.get("publicName")),
        user_type=_optional_string(value.get("type")),
    )
    return person if any((person.username, person.user_key, person.display_name)) else None


def _page_version(value: Any) -> PageVersion:
    if not isinstance(value, dict):
        raise Upstream("Confluence returned a malformed page version.")
    number = _positive_int(value.get("number"))
    if number is None:
        raise Upstream("Confluence returned a page version without a valid number.")
    minor_edit = value.get("minorEdit")
    return PageVersion(
        number=number,
        when=_optional_string(value.get("when")),
        author=_person(value.get("by")),
        message=_optional_string(value.get("message")),
        minor_edit=minor_edit if isinstance(minor_edit, bool) else None,
    )


def _first_dict(value: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, dict):
            return item
    return None


def _identifier(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
        return str(value).strip()
    return None


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise Upstream("Confluence returned an invalid numeric value.")


def _positive_int(value: Any) -> int | None:
    number = _optional_nonnegative_int(value)
    return number if number is not None and number > 0 else None


def _has_next_link(payload: dict[str, Any]) -> bool:
    links = payload.get("_links")
    return bool(isinstance(links, dict) and links.get("next")) or bool(payload.get("next"))


def _nonnegative_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise Upstream("Confluence returned an invalid paging value.")


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
