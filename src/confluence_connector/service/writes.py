"""Validated page, new-page draft and label writes for one account-bound Confluence service."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

from confluence_connector.domain.errors import (
    Conflict,
    ConnectorError,
    InvalidRequest,
)
from confluence_connector.domain.models import (
    DraftDeletionResult,
    DraftListResult,
    LabelChangeFailure,
    LabelChangeResult,
    PageWriteStatus,
    PageWriteResult,
    RemoteDraftPage,
    ResolvedPageReference,
)
from confluence_connector.domain.validation import (
    NOTIFICATION_SUPPRESSION_UNAVAILABLE,
    optional_space_key,
    page_reference,
    require_current_page,
    placement_inputs,
    validate_notify_watchers,
)
from confluence_connector.remote.confluence import DRAFT_PAGE_SIZE, ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service import cursors
from confluence_connector.service.placement import PagePlacementResolver
from confluence_connector.service.storage_files import exports_root, read_storage_file, split_export_header
from confluence_connector.service.write_markup import content_loss, file_to_storage, to_storage

MAX_TITLE_CHARS = 255
MAX_WRITE_BODY_CHARS = 2_000_000
MAX_DRAFT_OFFSET = 1_000_000
MAX_DRAFT_LIMIT = DRAFT_PAGE_SIZE
_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_DRAFT_CURSOR_KIND = "list_drafts"


class PageWrites:
    """Coordinate validated writes; public methods validate inputs before their remote operations."""

    def __init__(
        self,
        api: ConfluenceApi,
        links: PageLinkResolver,
        username: str,
        base_url: str,
    ) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._base_url = base_url.rstrip("/")
        self._placement_resolver = PagePlacementResolver(api, username)

    async def create_page(
        self,
        title: str,
        body: str | None = None,
        *,
        format: str = "rich",
        status: PageWriteStatus = "current",
        parent_id: str | None = None,
        space_key: str | None = None,
        position: str | None = None,
        sibling: str | None = None,
        body_file: str | None = None,
        body_file_roots: Sequence[Path] = (),
        notify_watchers: bool = True,
    ) -> PageWriteResult:
        """Create a current page or new-page draft under one validated placement.

        ``body_file`` is a storage file; a rich export's header is dropped and its body is treated as
        existing Confluence storage, so only the caller's new pre/details blocks are converted.
        """
        notify = validate_notify_watchers(notify_watchers)
        clean_title = _title(title)
        _one_body_source(body, body_file, format)
        if body_file is not None:
            header, file_body = split_export_header(_read_body_file(body_file, body_file_roots))
            storage_body = file_to_storage(file_body, file_body if header is not None else None)
        else:
            storage_body = _write_body(body, format)
        if not isinstance(status, str) or status not in {"current", "draft"}:
            raise InvalidRequest("status must be current or draft.")
        placement_kind, parent_reference, selected_space, validated_sibling = placement_inputs(
            parent_id=parent_id, space_key=space_key, position=position, sibling=sibling,
        )
        placement = await self._placement_resolver.resolve(
            parent_reference=parent_reference,
            space_key=selected_space,
            position=placement_kind,
            sibling_reference=validated_sibling,
            resolve_page=self._resolve_page,
        )
        await self._assert_title_available(clean_title, placement.space_key)
        created = await self._api.create_page(
            title=clean_title,
            space_key=placement.space_key,
            storage_body=storage_body,
            parent_id=placement.parent_id,
            status=status,
            expected_username=self._username,
        )
        warnings: list[str] = []
        action_position = _action_position(placement.position)
        if placement.sibling_id is not None and action_position is not None:
            try:
                await self._api.position_page(
                    created.page_id,
                    space_key=placement.space_key,
                    position=action_position,
                    target_id=placement.sibling_id,
                    expected_username=self._username,
                )
            except ConnectorError as exc:
                warnings.append(
                    f"The page was created, but its requested position was not confirmed "
                    f"({type(exc).__name__}); inspect the page before trying to reposition it."
                )
        return PageWriteResult(
            page_id=created.page_id,
            title=created.title,
            status=status,
            version=created.version,
            url=created.url,
            warnings=warnings,
            notify_watchers=notify,
            notification_suppression_supported=False,
            notification_note=NOTIFICATION_SUPPRESSION_UNAVAILABLE,
        )

    async def update_page(
        self,
        page: str,
        body: str | None = None,
        *,
        expected_version: int | None = None,
        format: str = "rich",
        title: str | None = None,
        allow_comment_loss: bool = False,
        body_file: str | None = None,
        body_file_roots: Sequence[Path] = (),
        notify_watchers: bool = True,
    ) -> PageWriteResult:
        """Replace a whole page body after checking its version and source references.

        The caller always names the target ``page``. A ``body_file`` header is editable text, so its
        page ID only has to agree with ``page``; it never selects the page. The header's version is the
        expected version unless ``expected_version`` is given, which must then agree with it.
        """
        notify = validate_notify_watchers(notify_watchers)
        reference = page_reference(page)
        _one_body_source(body, body_file, format)
        header = None
        file_body = ""
        if body_file is not None:
            header, file_body = split_export_header(_read_body_file(body_file, body_file_roots))
            if header is None:
                raise InvalidRequest(
                    "body_file must be a rich export_pages file with its header comment; text exports "
                    "and files without the header cannot update a page."
                )
            if expected_version is not None and _expected_version(expected_version) != header.version:
                raise InvalidRequest(
                    f"body_file was exported at version {header.version}, not expected_version "
                    f"{expected_version}."
                )
            version = header.version
            storage_body = ""
        else:
            if expected_version is None:
                raise InvalidRequest("expected_version is required with body.")
            version = _expected_version(expected_version)
            storage_body = _write_body(body, format)
        new_title = _title(title) if title is not None else None
        if not isinstance(allow_comment_loss, bool):
            raise InvalidRequest("allow_comment_loss must be a boolean.")
        resolved = await self._resolve_page(reference)
        if header is not None and resolved.page_id != header.page_id:
            raise InvalidRequest(
                f"body_file was exported from page {header.page_id}, not page {resolved.page_id}."
            )
        current = await self._api.get_page_content(
            resolved.page_id, expected_username=self._username, include_storage=True
        )
        require_current_page(current.metadata)
        if current.version != version:
            raise Conflict(
                f"The page is now version {current.version}; expected_version was {version}. "
                "Read it again and reapply the changes."
            )
        if header is not None:
            storage_body = file_to_storage(file_body, current.storage_body)
        final_title = new_title or current.metadata.title
        anchor_loss, warnings = content_loss(current.storage_body, storage_body)
        if anchor_loss and not allow_comment_loss:
            detail = " ".join(warnings)
            raise InvalidRequest(
                f"The update would detach inline comments. {detail} "
                "Pass allow_comment_loss=true to confirm this replacement."
            )
        if new_title is not None and new_title != current.metadata.title:
            if current.metadata.space_key is None:
                raise InvalidRequest("Confluence did not return the page's space, so its title cannot be checked.")
            existing_id = await self._find_page_by_title(new_title, current.metadata.space_key)
            if existing_id is not None and existing_id != current.metadata.page_id:
                raise InvalidRequest(
                    "A page with that title already exists in the target space: "
                    f"{self._page_url(current.metadata.space_key, existing_id)}"
                )
        updated = await self._api.update_page(
            current.metadata.page_id,
            title=final_title,
            storage_body=storage_body,
            next_version=version + 1,
            expected_username=self._username,
            notify_watchers=notify,
        )
        return PageWriteResult(
            page_id=updated.page_id,
            title=updated.title,
            status="current",
            version=updated.version or version + 1,
            url=updated.url or current.metadata.url,
            warnings=warnings,
            notify_watchers=notify,
            notification_suppression_supported=True,
        )

    async def list_drafts(
        self,
        *,
        space_key: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> DraftListResult:
        """List one bounded page of the account's new-page drafts with a self-contained cursor."""
        if cursor is not None:
            if space_key is not None or limit is not None:
                raise InvalidRequest("Pass only cursor; it already contains the draft list scope and limit.")
            selected_space, start, page_limit = _decode_draft_cursor(cursor)
        else:
            selected_space = optional_space_key(space_key)
            start = 0
            page_limit = _draft_limit(limit)
        page: RemoteDraftPage = await self._api.list_drafts_page(
            space_key=selected_space,
            start=start,
            limit=page_limit,
            expected_username=self._username,
        )
        next_start = page.next_start(page_limit)
        server_has_more = next_start is not None
        more = server_has_more and next_start <= MAX_DRAFT_OFFSET
        next_cursor = (
            cursors.encode(
                _DRAFT_CURSOR_KIND,
                space_key=selected_space,
                start=next_start,
                limit=page_limit,
            )
            if more
            else None
        )
        return DraftListResult(
            drafts=page.drafts,
            start=page.start,
            size=page.size,
            approximate_total=page.total_size,
            more_results_available=more,
            truncated=server_has_more and not more,
            next_cursor=next_cursor,
        )

    async def publish_draft(self, draft: str, *, notify_watchers: bool = True) -> PageWriteResult:
        """Publish one readable new-page draft while retaining its content ID."""
        notify = validate_notify_watchers(notify_watchers)
        reference = page_reference(draft, field="draft")
        resolved = await self._resolve_page(reference)
        source = await self._api.get_page_content(
            resolved.page_id,
            status="draft",
            include_storage=True,
            expected_username=self._username,
        )
        metadata = source.metadata
        if metadata.content_type != "page" or metadata.status != "draft":
            raise InvalidRequest("publish_draft accepts new-page drafts only.")
        if metadata.space_key is None:
            raise InvalidRequest("Confluence did not return the draft's space.")
        existing_id = await self._find_page_by_title(metadata.title, metadata.space_key)
        if existing_id is not None:
            raise InvalidRequest(
                "A current page with the draft's title already exists in its space: "
                f"{self._page_url(metadata.space_key, existing_id)}"
            )
        parent_id = metadata.ancestors[-1].page_id if metadata.ancestors else None
        published = await self._api.publish_draft(
            metadata.page_id,
            title=metadata.title,
            space_key=metadata.space_key,
            parent_id=parent_id,
            storage_body=source.storage_body,
            expected_username=self._username,
            notify_watchers=notify,
        )
        return PageWriteResult(
            page_id=published.page_id,
            title=published.title,
            status="current",
            version=published.version,
            url=published.url or metadata.url,
            notify_watchers=notify,
            notification_suppression_supported=True,
        )

    async def delete_draft(self, draft: str, *, notify_watchers: bool = True) -> DraftDeletionResult:
        """Permanently delete one verified new-page draft, bypassing the Confluence trash."""
        notify = validate_notify_watchers(notify_watchers)
        reference = page_reference(draft, field="draft")
        resolved = await self._resolve_page(reference)
        metadata = await self._api.get_page_metadata(
            resolved.page_id, status="draft", expected_username=self._username
        )
        if metadata.content_type != "page" or metadata.status != "draft":
            raise InvalidRequest("delete_draft accepts new-page drafts only.")
        await self._api.delete_draft(metadata.page_id, expected_username=self._username)
        return DraftDeletionResult(
            page_id=metadata.page_id,
            title=metadata.title,
            deleted=True,
            notify_watchers=notify,
            notification_suppression_supported=False,
            notification_note=NOTIFICATION_SUPPRESSION_UNAVAILABLE,
        )

    async def set_labels(
        self,
        page: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        notify_watchers: bool = True,
    ) -> LabelChangeResult:
        """Add and remove global labels, stopping at the first failed write with an exact partial result."""
        notify = validate_notify_watchers(notify_watchers)
        reference = page_reference(page)
        additions = _labels(add, "add")
        removals = _labels(remove, "remove")
        if not additions and not removals:
            raise InvalidRequest("Pass at least one label in add or remove.")
        if set(additions) & set(removals):
            raise InvalidRequest("A label cannot appear in both add and remove.")
        resolved = await self._resolve_page(reference)
        metadata = await self._api.get_page_metadata(
            resolved.page_id, expected_username=self._username
        )
        require_current_page(metadata)
        existing = set(metadata.labels)
        operations: list[tuple[str, str]] = []
        unchanged: list[str] = []
        for label in additions:
            if label in existing:
                unchanged.append(label)
            else:
                operations.append(("add", label))
        for label in removals:
            if label not in existing:
                unchanged.append(label)
            else:
                operations.append(("remove", label))
        result = LabelChangeResult(
            page_id=metadata.page_id,
            unchanged=unchanged,
            notify_watchers=notify,
            notification_suppression_supported=False,
            notification_note=NOTIFICATION_SUPPRESSION_UNAVAILABLE,
        )
        for index, (action, label) in enumerate(operations):
            try:
                if action == "add":
                    await self._api.add_page_label(
                        metadata.page_id, label, expected_username=self._username
                    )
                    result.added.append(label)
                else:
                    await self._api.remove_page_label(
                        metadata.page_id, label, expected_username=self._username
                    )
                    result.removed.append(label)
            except ConnectorError as exc:
                result.failures.append(
                    LabelChangeFailure(action=action, label=label, error=f"{type(exc).__name__}: {exc}")
                )
                result.not_attempted.extend(
                    LabelChangeFailure(
                        action=next_action,
                        label=next_label,
                        error="Not attempted after an earlier failure.",
                    )
                    for next_action, next_label in operations[index + 1 :]
                )
                break
        return result

    async def _resolve_page(self, reference: str) -> ResolvedPageReference:
        resolved = await self._links.resolve(reference, expected_username=self._username)
        if resolved.version is not None:
            raise InvalidRequest("Page writes target the current page; omit pageVersion from the reference.")
        if not resolved.page_id.isdecimal():
            raise InvalidRequest("The selected page reference did not resolve to a numeric page ID.")
        return resolved

    async def _assert_title_available(self, title: str, space_key: str) -> None:
        existing_id = await self._find_page_by_title(title, space_key)
        if existing_id is not None:
            raise InvalidRequest(
                "A page with that title already exists in the target space: "
                f"{self._page_url(space_key, existing_id)}"
            )

    async def _find_page_by_title(self, title: str, space_key: str) -> str | None:
        return await self._api.find_page_id_by_title(
            space_key, title, expected_username=self._username
        )

    def _page_url(self, space_key: str, page_id: str) -> str:
        return urljoin(
            self._base_url + "/",
            f"spaces/{quote(space_key, safe='')}/pages/{quote(page_id, safe='')}",
        )


def _title(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidRequest("title must be text.")
    result = value.strip()
    if not result or len(result) > MAX_TITLE_CHARS or any(not char.isprintable() for char in result):
        raise InvalidRequest(f"title must contain 1 to {MAX_TITLE_CHARS} printable characters.")
    return result


def _read_body_file(path: Any, client_roots: Sequence[Path]) -> str:
    """Read a body file from the exports folder or one of the client's shared MCP roots."""
    return read_storage_file(path, [exports_root(), *client_roots])


def _one_body_source(body: Any, body_file: Any, page_format: Any) -> None:
    if (body is None) == (body_file is None):
        raise InvalidRequest("Pass exactly one of body and body_file.")
    if body_file is not None and page_format != "rich":
        raise InvalidRequest("body_file holds rich storage markup; omit format or pass rich.")


def _write_body(value: Any, page_format: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_WRITE_BODY_CHARS:
        raise InvalidRequest(f"body must be text with at most {MAX_WRITE_BODY_CHARS} characters.")
    return to_storage(value, page_format)


def _expected_version(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest("expected_version must be a positive integer.")
    return value


def _action_position(position: str) -> str | None:
    if position in {"before", "first"}:
        return "above"
    if position == "after":
        return "below"
    return None


def _draft_limit(value: Any) -> int:
    result = 50 if value is None else value
    if not isinstance(result, int) or isinstance(result, bool) or not 1 <= result <= MAX_DRAFT_LIMIT:
        raise InvalidRequest(f"limit must be between 1 and {MAX_DRAFT_LIMIT}.")
    return result


def _decode_draft_cursor(value: str) -> tuple[str | None, int, int]:
    state = cursors.decode(value, _DRAFT_CURSOR_KIND)
    if set(state) != {"space_key", "start", "limit"}:
        raise InvalidRequest("Malformed draft cursor.")
    space_key = optional_space_key(state.get("space_key"))
    start = state.get("start")
    limit = state.get("limit")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not 0 <= start <= MAX_DRAFT_OFFSET
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_DRAFT_LIMIT
    ):
        raise InvalidRequest("Malformed draft cursor.")
    return space_key, start, limit


def _labels(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidRequest(f"{name} must be a list of label names.")
    result: list[str] = []
    seen: set[str] = set()
    for label in value:
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise InvalidRequest(f"{name} contains a label outside the allowed 1-255 character form.")
        if label in seen:
            raise InvalidRequest(f"{name} contains a duplicate label.")
        seen.add(label)
        result.append(label)
    return result
