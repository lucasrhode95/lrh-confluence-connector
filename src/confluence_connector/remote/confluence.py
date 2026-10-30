"""Documented Confluence REST routes and mapping into domain models."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Mapping
from typing import Any, BinaryIO
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from pydantic import ValidationError

from confluence_connector.domain.errors import (
    Conflict,
    ConnectorError,
    InvalidRequest,
    NotFound,
    UncertainWrite,
    Upstream,
)
from confluence_connector.domain.cql import cql_literal
from confluence_connector.domain.urls import same_instance_link, same_instance_origin
from confluence_connector.domain.models import (
    ContentAuthor,
    ContentSearchHit,
    CurrentUser,
    DraftSummary,
    PageEnvelope,
    PageMetadata,
    PageAttachment,
    RemotePageComment,
    RemotePageContent,
    PageSummary,
    PageVersion,
    PersonSummary,
    RemoteContentSearchPage,
    RemoteDraftPage,
    RemoteTrashPage,
    RemoteUserSearchPage,
    SpaceList,
    SpaceSummary,
    TrashPageSummary,
)
from confluence_connector.remote.transport import ConfluenceTransport

SPACE_PAGE_SIZE = 500
CHILD_PAGE_SIZE = 500
VERSION_PAGE_SIZE = 200
MAX_PAGE_VERSIONS = 5_000
MAX_COLLECTION_ITEMS = 5_000
DRAFT_PAGE_SIZE = 100
TRASH_PAGE_SIZE = 100
LONGTASK_MAX_POLLS = 40
LONGTASK_POLL_INTERVAL_SECONDS = 0.25
LONGTASK_MAX_WAIT_SECONDS = 10.0
_QUICKNAV_CONTENT_TYPES = {"content-type-page": "page", "content-type-blogpost": "blogpost"}
PLACEMENT_PROPERTY_KEY = "lrh-confluence-connector-placement"


class _LongTaskFailure(Conflict):
    """A completed async move explicitly reported failure, rather than an unknown outcome."""


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
                raw_items, envelope = _page_envelope(payload, requested_start=start)
                if envelope.total_size is not None:
                    reported_totals[status] = envelope.total_size
                    envelope.has_more = None  # The spaces endpoint reports an exact total.
                page = [self._space_summary(item) for item in raw_items]
                for space in page:
                    if space.key in seen_keys:
                        raise Upstream("Confluence repeated a space while paging the result.")
                    seen_keys.add(space.key)
                results.extend(page)
                returned_counts[status] += envelope.size
                next_start = envelope.next_start(SPACE_PAGE_SIZE)
                if next_start is None:
                    break
                start = next_start
        total_count = sum(
            reported if reported is not None else returned_counts[status]
            for status, reported in reported_totals.items()
        )
        total_count = max(total_count, len(results))
        return SpaceList(spaces=results, total_count=total_count, truncated=len(results) < total_count)

    async def find_page_by_title(
        self, space_key: str, title: str, *, expected_username: str, status: str | None = None
    ) -> str:
        """Resolve a legacy display URL by its validated space key and decoded title."""
        page_id = await self.find_page_id_by_title(
            space_key, title, expected_username=expected_username, status=status or "current"
        )
        if page_id is None:
            raise NotFound("The page in this display URL no longer matches a page; it may have been renamed.")
        return page_id

    async def find_page_id_by_title(
        self, space_key: str, title: str, *, expected_username: str, status: str = "current"
    ) -> str | None:
        """Resolve a validated exact title through the content collection, without CQL/index limitations."""
        if not space_key or not title:
            raise InvalidRequest("A display URL must include both a space key and page title.")
        payload = await self._transport.get_json(
            self._content_collection_route(),
            params={"type": "page", "spaceKey": space_key, "title": title, "status": status, "limit": "2"},
            expected_username=expected_username,
        )
        rows = _result_rows(payload)
        if len(rows) > 1 or _has_next_link(payload):
            raise Upstream("The display URL matched more than one page in its space.")
        if not rows:
            return None
        if not isinstance(rows[0], dict) or not _page_id(rows[0].get("id")):
            raise Upstream("Confluence returned a page-title match without a usable page ID.")
        return str(rows[0]["id"])

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
            payload, requested_start=start, base_url=str(self._settings["base_url"]), highlighted=True
        )

    async def quicknav_title_matches(
        self, query: str, *, limit: int, expected_username: str
    ) -> list[ContentSearchHit]:
        """Read the browser-observed quick-navigation title source without trusting returned URLs.

        The UI's ``src`` parameter makes the route return no matches; rows arrive in groups.
        """
        payload = await self._transport.get_json(
            "rest/quicknav/1/search",
            params={"query": query, "limit": str(limit), "type": "page"},
            expected_username=expected_username,
        )
        groups = payload.get("contentNameMatches")
        if not isinstance(groups, list) or not all(isinstance(group, list) for group in groups):
            raise Upstream("Confluence returned an unexpected quick-navigation response.")
        matches: list[ContentSearchHit] = []
        for group in groups:
            for value in group:
                hit = _quicknav_hit(value, base_url=str(self._settings["base_url"]))
                if hit is not None:
                    matches.append(hit)
        return matches

    async def search_users_page(
        self, name: str, *, start: int, limit: int, expected_username: str
    ) -> RemoteUserSearchPage:
        """Resolve a current user's display name through the observed user CQL search."""
        cql = f"type = user AND user.fullname ~ {cql_literal(name)}"
        payload = await self._transport.get_json(
            self._endpoint("ui_search"),
            params={"cql": cql, "start": str(start), "limit": str(limit)},
            expected_username=expected_username,
        )
        page = _content_search_page(
            payload, requested_start=start, base_url=str(self._settings["base_url"]), highlighted=True
        )
        users: list[PersonSummary] = []
        for row in _result_rows(payload):
            user = _person(_first_dict(row, "user", "entity"))
            if user is None:
                user = PersonSummary(
                    username=_optional_string(row.get("username")),
                    user_key=_optional_string(row.get("userKey")),
                    display_name=_search_snippet(row.get("title")),
                    user_type=_optional_string(row.get("type")),
                )
            if user.username or user.user_key or user.display_name:
                users.append(user)
        return RemoteUserSearchPage(
            users=users,
            start=page.start,
            size=page.size,
            total_size=page.total_size,
            has_more=page.has_more,
        )

    async def get_user(self, username: str, *, expected_username: str) -> PersonSummary:
        """Read a user by exact username, including former users absent from name search."""
        payload = await self._transport.get_json(
            "rest/api/user",
            params={"username": username},
            expected_username=expected_username,
        )
        user = _person(payload)
        if user is None or not (user.username or user.user_key):
            raise Upstream("Confluence returned a user without a username or user key.")
        return user

    async def get_page_metadata(
        self, page_id: str, *, expected_username: str, status: str | None = None
    ) -> PageMetadata:
        """Read one page's expanded history, ancestors and labels; ``page_id`` is service-validated."""
        params = {"expand": "space,version,history,ancestors,metadata.labels,history.contributors"}
        if status is not None:
            params["status"] = status
        payload = await self._transport.get_json(
            self._content_route(page_id),
            params=params,
            expected_username=expected_username,
        )
        metadata = _page_metadata(payload, base_url=str(self._settings["base_url"]))
        raw_metadata = payload.get("metadata")
        raw_labels = raw_metadata.get("labels") if isinstance(raw_metadata, dict) else None
        if isinstance(raw_labels, dict) and _has_next_link(raw_labels):
            labels: list[str] = []
            start = 0
            while True:
                label_page = await self._transport.get_json(
                    self._content_route(page_id) + "/label",
                    params={"start": str(start), "limit": "200"},
                    expected_username=expected_username,
                )
                rows = _result_rows(label_page)
                page_start = _nonnegative_int(label_page.get("start"), default=start)
                size = _nonnegative_int(label_page.get("size"), default=len(rows))
                if page_start != start or size != len(rows):
                    raise Upstream("Confluence returned inconsistent label paging.")
                labels.extend(_labels(rows))
                if not _has_next_link(label_page):
                    break
                if not size or start + size > 100_000:
                    raise Upstream("Confluence did not complete label paging within the safety bound.")
                start += size
            metadata.labels = list(dict.fromkeys(labels))
        return metadata

    async def page_supports_delete(self, page_id: str, *, expected_username: str) -> bool:
        """Read the current user's page operations; the caller validates ``page_id``."""
        payload = await self._transport.get_json(
            self._content_route(page_id),
            params={"expand": "operations"},
            expected_username=expected_username,
        )
        operations = payload.get("operations")
        if not isinstance(operations, list):
            raise Upstream("Confluence did not return page operations for the delete preflight.")
        for operation in operations:
            if isinstance(operation, dict) and operation.get("operation") == "delete":
                return True
        return False

    async def space_permissions_readable(self, space_key: str, *, expected_username: str) -> bool:
        """Check whether the account can read space permissions, without interpreting their policy."""
        route = f"rest/api/space/{quote(space_key, safe='')}"
        payload = await self._transport.get_json(
            route,
            params={"expand": "permissions"},
            expected_username=expected_username,
        )
        permissions = payload.get("permissions")
        if isinstance(permissions, dict):
            permissions = permissions.get("data")  # Data Center 9 wraps the list (research §6.4).
        if not isinstance(permissions, list):
            raise Upstream("Confluence returned no readable space-permission list.")
        return True

    async def trash_page(self, page_id: str, *, expected_username: str) -> None:
        """Move one validated page to the trash with the documented DELETE route."""
        await self._transport.send_json(
            "DELETE", self._content_route(page_id), expected_username=expected_username
        )

    async def restore_trashed_page(
        self,
        page_id: str,
        *,
        title: str,
        space_key: str,
        next_version: int,
        expected_username: str,
    ) -> PageMetadata:
        """Restore one trashed page; Confluence places restored pages at the space root."""
        payload = await self._transport.send_json(
            "PUT",
            self._content_route(page_id),
            params={"status": "trashed"},
            body={
                "id": page_id,
                "type": "page",
                "title": title,
                "status": "current",
                "version": {"number": next_version},
                "space": {"key": space_key},
            },
            expected_username=expected_username,
        )
        return _write_page_metadata(payload, base_url=str(self._settings["base_url"]))

    async def get_page_property(
        self, page_id: str, key: str, *, expected_username: str
    ) -> Any:
        """Read one page property; the service validates its key and page reference."""
        payload = await self._transport.get_json(
            f"{self._content_route(page_id)}/property/{quote(key, safe='')}",
            expected_username=expected_username,
        )
        if "value" not in payload:
            raise Upstream("Confluence returned a page property without its value.")
        return payload["value"]

    async def set_page_property(
        self, page_id: str, key: str, value: Mapping[str, Any], *, expected_username: str
    ) -> None:
        """Create or update a small JSON placement record without changing the page version."""
        property_route = f"{self._content_route(page_id)}/property/{quote(key, safe='')}"
        try:
            existing = await self._transport.get_json(
                property_route, expected_username=expected_username
            )
        except NotFound:
            await self._transport.send_json(
                "POST",
                f"{self._content_route(page_id)}/property",
                body={"key": key, "value": dict(value)},
                expected_username=expected_username,
            )
            return
        property_id = _identifier(existing.get("id"))
        version = existing.get("version")
        version_number = (
            _optional_nonnegative_int(version.get("number")) if isinstance(version, dict) else None
        )
        if property_id is None or version_number is None or version_number < 1:
            raise Upstream("Confluence returned an incomplete placement property for update.")
        await self._transport.send_json(
            "PUT",
            property_route,
            body={
                "id": property_id,
                "key": key,
                "value": dict(value),
                "version": {"number": version_number + 1},
            },
            expected_username=expected_username,
        )

    async def remove_page_property(self, page_id: str, key: str, *, expected_username: str) -> None:
        """Remove the connector-owned placement property after a successful restore."""
        await self._transport.send_json(
            "DELETE",
            f"{self._content_route(page_id)}/property/{quote(key, safe='')}",
            expected_username=expected_username,
        )

    async def list_trash_page(
        self,
        *,
        space_key: str | None,
        start: int,
        limit: int,
        expected_username: str,
    ) -> RemoteTrashPage:
        """Read one bounded page of visible trashed pages from the content collection."""
        rows, envelope = await self._list_page_status(
            "trashed", space_key=space_key, start=start, limit=limit, expand="space,version",
            expected_username=expected_username,
        )
        return RemoteTrashPage(
            pages=[_trash_summary(row, base_url=str(self._settings["base_url"])) for row in rows],
            **envelope.model_dump(),
        )

    async def _list_page_status(
        self, status: str, *, space_key: str | None, start: int, limit: int,
        expand: str, expected_username: str,
    ) -> tuple[list[Any], PageEnvelope]:
        """Read a boundary-validated draft/trash request through the shared content collection."""
        params = {"type": "page", "status": status, "start": str(start), "limit": str(limit), "expand": expand}
        if space_key is not None:
            params["spaceKey"] = space_key
        payload = await self._transport.get_json(
            self._content_collection_route(), params=params, expected_username=expected_username
        )
        return _page_envelope(payload, requested_start=start)

    async def get_page_content(
        self,
        page_id: str,
        *,
        version: int | None = None,
        status: str | None = None,
        include_storage: bool = True,
        expected_username: str,
    ) -> RemotePageContent:
        """Read page view/storage bodies and provenance, optionally pinning a historical version.

        ``page_id`` and ``version`` are validated by the page-reading service before this adapter is
        called. Historical reads use the verified Data Center query contract in API research.
        """
        expansions = "space,version,history,ancestors,metadata.labels,body.view"
        if include_storage:
            expansions += ",body.storage"
        params = {"expand": expansions}
        if version is not None:
            params.update({"status": "historical", "version": str(version)})
        elif status is not None:
            params["status"] = status
        payload = await self._transport.get_json(
            self._content_route(page_id), params=params, expected_username=expected_username
        )
        return _page_content(
            payload,
            base_url=str(self._settings["base_url"]),
            requested_version=version,
            include_storage=include_storage,
        )

    async def create_page(
        self,
        *,
        title: str,
        space_key: str,
        storage_body: str,
        parent_id: str | None,
        status: str,
        expected_username: str,
    ) -> PageMetadata:
        """Create one current page or new-page draft from validated service inputs."""
        body: dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "status": status,
            "body": {"storage": {"value": storage_body, "representation": "storage"}},
        }
        if parent_id is not None:
            body["ancestors"] = [{"id": parent_id}]
        payload = await self._transport.send_json(
            "POST",
            self._content_collection_route(),
            body=body,
            expected_username=expected_username,
        )
        return _write_page_metadata(payload, base_url=str(self._settings["base_url"]))

    async def update_page(
        self,
        page_id: str,
        *,
        title: str,
        storage_body: str,
        next_version: int,
        expected_username: str,
    ) -> PageMetadata:
        """Replace a validated current page's title and complete storage body once."""
        payload = await self._transport.send_json(
            "PUT",
            self._content_route(page_id),
            body={
                "id": page_id,
                "type": "page",
                "title": title,
                "version": {"number": next_version},
                "body": {"storage": {"value": storage_body, "representation": "storage"}},
            },
            expected_username=expected_username,
        )
        return _write_page_metadata(payload, base_url=str(self._settings["base_url"]))

    async def list_drafts_page(
        self,
        *,
        space_key: str | None,
        start: int,
        limit: int,
        expected_username: str,
    ) -> RemoteDraftPage:
        """Read one bounded page of new-page drafts; caller validates scope and cursor state."""
        rows, envelope = await self._list_page_status(
            "draft", space_key=space_key, start=start, limit=limit, expand="space,version,history",
            expected_username=expected_username,
        )
        return RemoteDraftPage(
            drafts=[_draft_summary(row, base_url=str(self._settings["base_url"])) for row in rows],
            **envelope.model_dump(),
        )

    async def publish_draft(
        self,
        page_id: str,
        *,
        title: str,
        space_key: str,
        parent_id: str | None,
        storage_body: str,
        expected_username: str,
    ) -> PageMetadata:
        """Publish a validated new-page draft under the same ID."""
        body: dict[str, Any] = {
            "id": page_id,
            "type": "page",
            "title": title,
            "status": "current",
            "version": {"number": 1},
            "space": {"key": space_key},
            "body": {"storage": {"value": storage_body, "representation": "storage"}},
        }
        if parent_id is not None:
            body["ancestors"] = [{"id": parent_id}]
        payload = await self._transport.send_json(
            "PUT", self._content_route(page_id), body=body, expected_username=expected_username
        )
        return _write_page_metadata(payload, base_url=str(self._settings["base_url"]))

    async def delete_draft(self, page_id: str, *, expected_username: str) -> None:
        """Permanently delete one validated new-page draft through its draft status route."""
        await self._transport.send_json(
            "DELETE",
            self._content_route(page_id),
            params={"status": "draft"},
            expected_username=expected_username,
        )

    async def add_page_label(self, page_id: str, label: str, *, expected_username: str) -> None:
        """Add one validated global label to a page."""
        await self._transport.send_json(
            "POST",
            f"{self._content_route(page_id)}/label",
            body=[{"prefix": "global", "name": label}],
            expected_username=expected_username,
        )

    async def remove_page_label(self, page_id: str, label: str, *, expected_username: str) -> None:
        """Remove one validated global label from a page."""
        await self._transport.send_json(
            "DELETE",
            f"{self._content_route(page_id)}/label/{quote(label, safe='')}",
            expected_username=expected_username,
        )

    async def position_page(
        self,
        page_id: str,
        *,
        space_key: str,
        position: str,
        target_id: str,
        expected_username: str,
    ) -> None:
        """Use Confluence's isolated UI move action to place a page above or below a sibling."""
        await self.move_page_action(
            page_id, space_key=space_key, position=position, target_id=target_id,
            asynchronous=False, expected_username=expected_username,
        )

    async def move_page_action(
        self,
        page_id: str,
        *,
        space_key: str,
        position: str,
        target_id: str | None,
        asynchronous: bool,
        expected_username: str,
    ) -> None:
        """Run the isolated UI move action and, for cross-space moves, poll its long task."""
        params = {
            "pageId": page_id,
            "spaceKey": space_key,
            "position": position,
            "mode": "ASYNC" if asynchronous else "LEGACY",
        }
        if target_id is not None:
            params["targetId"] = target_id
        payload = await self._transport.send_json(
            "POST",
            "pages/movepage.action",
            params=params,
            skip_atlassian_token_check=True,
            expected_username=expected_username,
        )
        has_task = isinstance(payload, dict) and isinstance(payload.get("longTaskId"), str)
        _raise_move_action_errors(payload, allow_pending=asynchronous and has_task)
        if not asynchronous:
            return
        if not has_task:
            raise UncertainWrite("Confluence accepted a move without a usable long-task ID; inspect the page.")
        try:
            async with asyncio.timeout(LONGTASK_MAX_WAIT_SECONDS):
                await self._wait_for_long_task(payload["longTaskId"], expected_username=expected_username)
        except _LongTaskFailure:
            raise
        except UncertainWrite:
            raise
        except TimeoutError:
            raise UncertainWrite(
                "Confluence's page move is still pending; inspect the page before retrying."
            ) from None
        except ConnectorError:
            raise UncertainWrite(
                "Confluence accepted the page move, but its completion could not be confirmed; inspect before retrying."
            ) from None

    async def _wait_for_long_task(self, task_id: str, *, expected_username: str) -> None:
        """Poll a documented long-task route a bounded number of times after an accepted async move."""
        route = f"rest/api/longtask/{quote(task_id, safe='')}"
        for attempt in range(LONGTASK_MAX_POLLS):
            payload = await self._transport.get_json(route, expected_username=expected_username)
            if not isinstance(payload, dict):
                raise UncertainWrite("Confluence returned an unreadable move status; inspect the page.")
            successful = payload.get("successful")
            finished = payload.get("finished")
            # successful describes progress too; it does not establish completion.
            complete = finished is True or (
                finished is None and payload.get("percentageComplete") == 100
            )
            if complete:
                if successful is True:
                    return
                if successful is False:
                    raise _LongTaskFailure("Confluence's asynchronous page move did not complete successfully.")
                raise UncertainWrite("Confluence returned an unreadable terminal move status; inspect the page.")
            if attempt + 1 < LONGTASK_MAX_POLLS:
                await asyncio.sleep(LONGTASK_POLL_INTERVAL_SECONDS)
        raise UncertainWrite("Confluence's page move is still pending; inspect the page before retrying.")

    async def list_page_attachments(
        self, page_id: str, *, expected_username: str
    ) -> list[PageAttachment]:
        """Read the documented attachment child list; the service validates ``page_id``."""
        rows = await self._collection_rows(
            f"{self._content_route(page_id)}/child/attachment", expected_username=expected_username
        )
        attachments = []
        for row in rows:
            attachment = _page_attachment(row, base_url=str(self._settings["base_url"]))
            if attachment is None:
                raise Upstream("Confluence returned an attachment without its ID or filename.")
            attachments.append(attachment)
        return attachments

    async def download_attachment(
        self,
        attachment: PageAttachment,
        destination: BinaryIO,
        *,
        expected_username: str,
    ) -> tuple[str | None, int]:
        """Stream an attachment through its mapped same-instance download link.

        The service passes an attachment mapped from the configured instance's child list; this
        adapter still validates origin and installation context before creating a relative route.
        """
        if not attachment.download_url:
            raise Upstream("Confluence did not provide a safe download link for this attachment.")
        path, params = _attachment_download_route(str(self._settings["base_url"]), attachment.download_url)
        return await self._transport.download_to(
            path,
            destination,
            params=params,
            expected_username=expected_username,
        )

    async def list_page_comments(
        self, page_id: str, *, expected_username: str
    ) -> list[RemotePageComment]:
        """Read footer and inline comments with replies, paging to the collection safety cap."""
        rows = await self._collection_rows(
            f"{self._content_route(page_id)}/child/comment",
            params={"depth": "all", "expand": "body.view,history"}, expected_username=expected_username,
        )
        comments = []
        for row in rows:
            comment = _page_comment(row)
            if comment is None:
                raise Upstream("Confluence returned a comment without its ID.")
            comments.append(comment)
        return comments

    async def _collection_rows(
        self, route: str, *, expected_username: str, params: Mapping[str, str] | None = None
    ) -> list[Any]:
        """Read a complete attachment/comment collection or explicitly fail at the 5,000-item cap."""
        rows: list[Any] = []
        start = 0
        while len(rows) < MAX_COLLECTION_ITEMS:
            payload = await self._transport.get_json(
                route, params={**(params or {}), "start": str(start), "limit": "500"},
                expected_username=expected_username,
            )
            items, envelope = _page_envelope(payload, requested_start=start)
            rows.extend(items)
            next_start = envelope.next_start(500, exact_total=True)
            if next_start is None:
                if len(rows) > MAX_COLLECTION_ITEMS:
                    break
                return rows
            start = next_start
        raise Upstream("The attachment/comment collection exceeds the 5,000-item safety cap and is incomplete.")

    async def create_page_comment(
        self,
        page_id: str,
        storage_body: str,
        *,
        expected_username: str,
        parent_comment_id: str | None = None,
    ) -> str:
        """Create a fixture footer comment or reply from trusted synthetic storage content."""
        if not page_id.isdecimal() or (
            parent_comment_id is not None and not parent_comment_id.isdecimal()
        ):
            raise InvalidRequest("Page and parent comment IDs must be decimal identifiers.")
        body: dict[str, Any] = {
            "type": "comment",
            "container": {"id": page_id, "type": "page"},
            "body": {"storage": {"value": storage_body, "representation": "storage"}},
        }
        if parent_comment_id is not None:
            body["ancestors"] = [{"id": parent_comment_id}]
        payload = await self._transport.send_json(
            "POST",
            self._content_collection_route(),
            body=body,
            expected_username=expected_username,
        )
        comment_id = _identifier(payload.get("id")) if isinstance(payload, dict) else None
        if comment_id is None or not comment_id.isdecimal():
            raise UncertainWrite(
                "Confluence may have accepted the fixture comment but returned no usable ID; inspect before retrying."
            )
        return comment_id

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
            rows, envelope = _page_envelope(payload, requested_start=start)
            versions.extend(_page_version(row) for row in rows)
            if envelope.total_size is not None:
                envelope.has_more = None  # Version-list totals are exact, unlike CQL search totals.
            next_start = envelope.next_start(VERSION_PAGE_SIZE)
            if next_start is None:
                return versions[:MAX_PAGE_VERSIONS], len(versions) > MAX_PAGE_VERSIONS
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
            params={"start": str(start), "limit": str(CHILD_PAGE_SIZE), "depth": "root"},
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

    def _content_collection_route(self) -> str:
        """Derive the configured content collection route from its page route template."""
        template = self._endpoint("content")
        marker = "{page_id}"
        if marker not in template:
            raise Upstream("The configured Confluence content endpoint has no page ID placeholder.")
        return template.split(marker, maxsplit=1)[0].rstrip("/")

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
            url=same_instance_link(str(self._settings["base_url"]), webui),
        )


def _content_search_page(
    payload: dict[str, Any], *, requested_start: int, base_url: str, highlighted: bool = False
) -> RemoteContentSearchPage:
    """Validate the server page envelope and map source rows before service-level processing.

    ``highlighted`` marks search-API rows, whose own title and excerpt are highlighted HTML snippets.
    """
    rows, envelope = _page_envelope(payload, requested_start=requested_start)
    return RemoteContentSearchPage(
        hits=[_content_search_hit(row, base_url=base_url, highlighted=highlighted) for row in rows],
        **envelope.model_dump(),
    )


def _page_envelope(payload: dict[str, Any], *, requested_start: int) -> tuple[list[Any], PageEnvelope]:
    """Validate one collection envelope at the remote boundary, before any consumer sees it."""
    rows = _result_rows(payload)
    start = _nonnegative_int(payload.get("start"), default=requested_start)
    size = _nonnegative_int(payload.get("size"), default=len(rows))
    if start != requested_start or size != len(rows):
        raise Upstream("Confluence returned an inconsistent collection page offset or size.")
    envelope = PageEnvelope(
        start=start, size=size,
        total_size=_optional_nonnegative_int(payload.get("totalSize", payload.get("totalCount"))),
        # A next marker anywhere continues; links without one end the page only when present.
        has_more=True if _has_next_link(payload) else (False if "_links" in payload else None),
    )
    # Explicit continuations/totals cannot make progress through an empty page.
    envelope.next_start(max(1, size))
    return rows, envelope


def _result_rows(payload: dict[str, Any]) -> list[Any]:
    """Return an API results array, failing explicitly when the response envelope is malformed."""
    rows = payload.get("results")
    if not isinstance(rows, list):
        raise Upstream("Confluence returned an unexpected search response.")
    return rows


def _content_search_hit(value: Any, *, base_url: str, highlighted: bool) -> ContentSearchHit:
    """Map a search or child-content source row without inferring fields from rendered text."""
    if not isinstance(value, dict):
        raise Upstream("Confluence returned a malformed content-search result.")
    nested = _first_dict(value, "content", "entity")
    content = nested or value
    content_id = _identifier(content.get("id", value.get("id", value.get("contentId"))))
    if highlighted:
        # A search row's own title is a highlighted HTML snippet; its nested record's title is plain.
        title = (_optional_string(nested.get("title")) if nested else None) or _search_snippet(value.get("title"))
    else:
        title = _optional_string(value.get("title"))
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
    position = _optional_position(content.get("position", extensions.get("position")))
    raw_url = _optional_string(value.get("url")) or _optional_string(links.get("webui"))
    return ContentSearchHit(
        content_id=content_id,
        content_type=content_type,
        title=title,
        space_key=_optional_string(space.get("key")),
        space_name=_optional_string(space.get("name")),
        url=same_instance_link(base_url, raw_url),
        excerpt=_search_snippet(value.get("excerpt")) if highlighted else _optional_string(value.get("excerpt")),
        modified_at=_optional_string(version.get("when")) or _optional_string(value.get("lastModified")),
        last_modified_by=_content_author(version.get("by")),
        parent_id=ancestors[-1].page_id if ancestors else None,
        ancestors=ancestors,
        position=position,
        sources=["cql"],
    )


def _search_snippet(value: Any) -> str | None:
    """Return search-row text without Confluence's highlight delimiters or HTML entity escaping."""
    text = _optional_string(value)
    if text is None:
        return None
    return _optional_string(html.unescape(text.replace("@@@hl@@@", "").replace("@@@endhl@@@", "")))


def _quicknav_hit(value: Any, *, base_url: str) -> ContentSearchHit | None:
    """Map only quick-navigation page or blog rows that provide a usable source id and title."""
    if not isinstance(value, dict):
        return None
    content_type = _QUICKNAV_CONTENT_TYPES.get(value.get("className"))
    content_id = _identifier(value.get("id"))
    title = _optional_string(value.get("name"))
    if content_type is None or content_id is None or not content_id.isdecimal() or title is None:
        return None
    return ContentSearchHit(
        content_id=content_id,
        content_type=content_type,
        title=title,
        space_key=_optional_string(value.get("spaceKey")),
        space_name=_optional_string(value.get("spaceName")),
        url=same_instance_link(base_url, _optional_string(value.get("href"))),
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
        url=same_instance_link(base_url, links.get("webui")),
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


def _write_page_metadata(payload: Any, *, base_url: str) -> PageMetadata:
    """Map a successful write response, reporting unusable success bodies as uncertain writes."""
    if not isinstance(payload, dict):
        raise UncertainWrite(
            "Confluence returned success for a page write without usable page details; inspect before retrying."
        )
    try:
        return _page_metadata(payload, base_url=base_url)
    except (Upstream, ValidationError):
        raise UncertainWrite(
            "Confluence returned success for a page write without usable page details; inspect before retrying."
        ) from None


def _draft_summary(value: Any, *, base_url: str) -> DraftSummary:
    """Map one new-page draft from its source content fields, rejecting other content types."""
    if not isinstance(value, dict):
        raise Upstream("Confluence returned a malformed page draft.")
    metadata = _page_metadata(value, base_url=base_url)
    if metadata.content_type != "page" or metadata.status not in (None, "draft"):
        raise Upstream("Confluence returned content that was not a new-page draft.")
    return DraftSummary(
        page_id=metadata.page_id,
        title=metadata.title,
        space_key=metadata.space_key,
        space_name=metadata.space_name,
        url=_draft_url(metadata, base_url=base_url),
        modified_at=metadata.modified_at,
    )


def _draft_url(metadata: PageMetadata, *, base_url: str) -> str:
    """Build an ID-based URL that the page-reference resolver can resolve for this draft."""
    instance_root = base_url.rstrip("/")
    page_id = quote(metadata.page_id, safe="")
    if metadata.space_key is not None:
        space_key = quote(metadata.space_key, safe="")
        return f"{instance_root}/spaces/{space_key}/pages/{page_id}"
    return f"{instance_root}/pages/viewpage.action?pageId={page_id}"


def _trash_summary(value: Any, *, base_url: str) -> TrashPageSummary:
    """Map one trashed page from source metadata, rejecting malformed or unrelated content."""
    if not isinstance(value, dict):
        raise Upstream("Confluence returned a malformed trashed-page entry.")
    metadata = _page_metadata(value, base_url=base_url)
    if metadata.content_type != "page" or metadata.status not in (None, "trashed"):
        raise Upstream("Confluence returned content that was not a trashed page.")
    return TrashPageSummary(
        page_id=metadata.page_id,
        title=metadata.title,
        space_key=metadata.space_key,
        space_name=metadata.space_name,
        url=metadata.url,
        modified_at=metadata.modified_at,
    )


def _raise_move_action_errors(payload: Any, *, allow_pending: bool = False) -> None:
    """Translate application-level errors in a successful UI-move response."""
    if not isinstance(payload, dict):
        raise Upstream("Confluence returned an unexpected page-position response.")
    delegates = payload.get("commandDelegate")
    responses = [payload]
    if isinstance(delegates, dict):
        responses.append(delegates)
    if any(
        response.get("validationErrors")
        or response.get("actionErrors")
        or response.get("errorMessage")
        or response.get("success") is False
        or (not allow_pending and response.get("successful") is False)
        for response in responses
    ):
        raise Upstream("Confluence rejected the requested page position.")


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
                url=same_instance_link(base_url, links.get("webui")),
                position=_optional_position(item.get("position")),
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
        publishers = value.get("publishers")
        if isinstance(publishers, dict):
            value = publishers.get("users", publishers.get("results"))
        else:
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


def _content_author(value: Any) -> ContentAuthor | None:
    """Map the latest-version author from source fields; absence is a valid null result."""
    person = _person(value)
    if person is None:
        return None
    return ContentAuthor(username=person.username, display_name=person.display_name)


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


def _page_content(
    payload: dict[str, Any],
    *,
    base_url: str,
    requested_version: int | None,
    include_storage: bool,
) -> RemotePageContent:
    """Map requested page expansions before body rendering; ``include_storage`` mirrors the request."""
    metadata = _page_metadata(payload, base_url=base_url)
    version = metadata.version
    if requested_version is not None and version is None:
        raise Upstream("Confluence did not confirm the requested historical page version.")
    if version is None:
        raise Upstream("Confluence returned page content without a version number.")
    if requested_version is not None and version != requested_version:
        raise Upstream("Confluence returned a different page version than the requested historical version.")
    body = payload.get("body")
    body = body if isinstance(body, dict) else {}
    view = body.get("view")
    view = view if isinstance(view, dict) else {}
    storage = body.get("storage")
    storage = storage if isinstance(storage, dict) else {}
    view_value = view.get("value")
    storage_value = storage.get("value")
    if (
        not isinstance(view_value, str)
        or (include_storage and not isinstance(storage_value, str))
        or (storage_value is not None and not isinstance(storage_value, str))
    ):
        raise Upstream(
            "Confluence returned page content without an expanded view or a valid requested storage body."
        )
    history = payload.get("history")
    history = history if isinstance(history, dict) else {}
    total_versions = _optional_nonnegative_int(
        history.get("totalVersions", history.get("numberOfVersions"))
    )
    return RemotePageContent(
        metadata=metadata,
        view_body=view_value,
        storage_body=storage_value if isinstance(storage_value, str) else "",
        version=version,
        total_versions=total_versions,
    )


def _page_attachment(value: Any, *, base_url: str) -> PageAttachment | None:
    """Map a child attachment only from its source identity and media fields."""
    if not isinstance(value, dict):
        return None
    attachment_id = _identifier(value.get("id"))
    filename = _optional_string(value.get("title")) or _optional_string(value.get("filename"))
    if attachment_id is None or filename is None:
        return None
    extensions = value.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    links = value.get("_links")
    links = links if isinstance(links, dict) else {}
    return PageAttachment(
        attachment_id=attachment_id,
        filename=filename,
        media_type=_optional_string(value.get("mediaType")) or _optional_string(extensions.get("mediaType")),
        file_size=_optional_nonnegative_int(value.get("fileSize", extensions.get("fileSize"))),
        download_url=same_instance_link(base_url, links.get("download")),
    )


def _page_comment(value: Any) -> RemotePageComment | None:
    """Map the requested expanded comment view and provenance from source fields."""
    if not isinstance(value, dict):
        return None
    comment_id = _identifier(value.get("id"))
    if comment_id is None:
        return None
    body = value.get("body")
    if not isinstance(body, dict):
        raise Upstream("Confluence returned a comment without its expanded view body.")
    view = body.get("view")
    if not isinstance(view, dict) or not isinstance(view.get("value"), str):
        raise Upstream("Confluence returned a comment without a valid expanded view body.")
    view_body = view["value"]
    history = value.get("history")
    history = history if isinstance(history, dict) else {}
    extensions = value.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    inline_properties = extensions.get("inlineProperties")
    inline_properties = inline_properties if isinstance(inline_properties, dict) else {}
    ancestors = value.get("ancestors")
    ancestors = ancestors if isinstance(ancestors, list) else []
    parent_comment_id = next(
        (
            identifier
            for ancestor in reversed(ancestors)
            if isinstance(ancestor, dict)
            and ancestor.get("type") == "comment"
            and (identifier := _identifier(ancestor.get("id"))) is not None
        ),
        None,
    )
    author = _person(history.get("createdBy")) or _person(value.get("author"))
    return RemotePageComment(
        comment_id=comment_id,
        parent_comment_id=parent_comment_id,
        author=author,
        created_at=_optional_string(history.get("createdDate")),
        body_view=view_body,
        location=_optional_string(extensions.get("location")),
        resolution=_optional_string(extensions.get("resolutionStatus"))
        or _optional_string(extensions.get("resolution")),
        selected_text=_optional_string(inline_properties.get("originalSelection")),
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


def _optional_position(value: Any) -> int | None:
    """Map the server's unordered-position sentinels without weakening numeric count validation."""
    if value in (-1, "-1", "none"):
        return None
    return _optional_nonnegative_int(value)


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


def _attachment_download_route(base_url: str, value: str) -> tuple[str, dict[str, str]]:
    """Validate a mapped download URL and reduce it to an in-context route and query."""
    base = urlsplit(base_url)
    link = urlsplit(value)
    if not same_instance_origin(base_url, value):
        raise Upstream("Confluence returned an unsafe attachment download link.")

    base_path = base.path.rstrip("/")
    if base_path:
        if not link.path.startswith(base_path + "/"):
            raise Upstream("Confluence returned an attachment link outside the configured context path.")
        path = link.path[len(base_path) + 1 :]
    else:
        path = link.path.lstrip("/")
    decoded_segments = [unquote(segment) for segment in path.split("/")]
    if (
        not path
        or "\\" in path
        or any(segment in {".", ".."} or "/" in segment or "\\" in segment for segment in decoded_segments)
    ):
        raise Upstream("Confluence returned an unsafe attachment download path.")
    return path, dict(parse_qsl(link.query, keep_blank_values=True))
