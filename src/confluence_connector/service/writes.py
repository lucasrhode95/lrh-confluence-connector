"""Validated page, new-page draft and label writes for one account-bound Confluence service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    Conflict,
    ConnectorError,
    InvalidRequest,
    Upstream,
)
from confluence_connector.domain.models import (
    DraftDeletionResult,
    DraftListResult,
    LabelChangeFailure,
    LabelChangeResult,
    PageMetadata,
    PageWriteStatus,
    PageWriteResult,
    RemoteDraftPage,
    ResolvedPageReference,
)
from confluence_connector.remote.confluence import DRAFT_PAGE_SIZE, ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service import cursors
from confluence_connector.service.write_markup import content_loss, to_storage

MAX_TITLE_CHARS = 255
MAX_WRITE_BODY_CHARS = 2_000_000
MAX_DRAFT_OFFSET = 1_000_000
MAX_DRAFT_LIMIT = DRAFT_PAGE_SIZE
_SPACE_KEY = re.compile(r"^(?:[A-Za-z0-9_-]{1,255}|~[A-Za-z0-9_-]{1,254})$")
_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_DRAFT_CURSOR_KIND = "list_drafts"


@dataclass(frozen=True)
class _Placement:
    space_key: str
    parent_id: str | None
    position: str
    target_id: str | None


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

    async def create_page(
        self,
        title: str,
        body: str,
        *,
        format: str = "rich",
        status: PageWriteStatus = "current",
        parent_id: str | None = None,
        space_key: str | None = None,
        position: str | None = None,
        sibling: str | None = None,
    ) -> PageWriteResult:
        """Create a current page or new-page draft under one validated placement."""
        clean_title = _title(title)
        storage_body = _write_body(body, format)
        if not isinstance(status, str) or status not in {"current", "draft"}:
            raise InvalidRequest("status must be current or draft.")
        placement_kind = _placement_kind(position, sibling)
        validated_sibling = _page_reference(sibling) if sibling is not None else None
        placement = await self._create_placement(
            parent_id=parent_id,
            space_key=space_key,
            position=placement_kind,
            sibling=validated_sibling,
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
        if placement.target_id is not None and action_position is not None:
            try:
                await self._api.position_page(
                    created.page_id,
                    space_key=placement.space_key,
                    position=action_position,
                    target_id=placement.target_id,
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
        )

    async def update_page(
        self,
        page: str,
        body: str,
        *,
        expected_version: int,
        format: str = "rich",
        title: str | None = None,
        allow_comment_loss: bool = False,
    ) -> PageWriteResult:
        """Replace a whole page body after checking its version and source references."""
        reference = _page_reference(page)
        storage_body = _write_body(body, format)
        version = _expected_version(expected_version)
        new_title = _title(title) if title is not None else None
        if not isinstance(allow_comment_loss, bool):
            raise InvalidRequest("allow_comment_loss must be a boolean.")
        resolved = await self._resolve_page(reference)
        current = await self._api.get_page_content(
            resolved.page_id, expected_username=self._username, include_storage=True
        )
        _require_current_page(current.metadata)
        if current.version != version:
            raise Conflict(
                f"The page is now version {current.version}; expected_version was {version}. "
                "Read it again and reapply the changes."
            )
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
        )
        return PageWriteResult(
            page_id=updated.page_id,
            title=updated.title,
            status="current",
            version=updated.version or version + 1,
            url=updated.url or current.metadata.url,
            warnings=warnings,
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
            selected_space = _optional_space_key(space_key)
            start = 0
            page_limit = _draft_limit(limit)
        page: RemoteDraftPage = await self._api.list_drafts_page(
            space_key=selected_space,
            start=start,
            limit=page_limit,
            expected_username=self._username,
        )
        next_start = page.start + page.size
        if page.has_more is not None:
            has_more = page.has_more
        elif page.total_size is not None:
            has_more = next_start < page.total_size
        else:
            has_more = page.size == page_limit
        server_has_more = page.size > 0 and has_more
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

    async def publish_draft(self, draft: str) -> PageWriteResult:
        """Publish one readable new-page draft while retaining its content ID."""
        reference = _page_reference(draft)
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
        )
        return PageWriteResult(
            page_id=published.page_id,
            title=published.title,
            status="current",
            version=published.version,
            url=published.url or metadata.url,
        )

    async def delete_draft(self, draft: str) -> DraftDeletionResult:
        """Permanently delete one verified new-page draft, bypassing the Confluence trash."""
        reference = _page_reference(draft)
        resolved = await self._resolve_page(reference)
        metadata = await self._api.get_page_metadata(
            resolved.page_id, status="draft", expected_username=self._username
        )
        if metadata.content_type != "page" or metadata.status != "draft":
            raise InvalidRequest("delete_draft accepts new-page drafts only.")
        await self._api.delete_draft(metadata.page_id, expected_username=self._username)
        return DraftDeletionResult(page_id=metadata.page_id, title=metadata.title, deleted=True)

    async def set_labels(
        self,
        page: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> LabelChangeResult:
        """Add and remove global labels, stopping at the first failed write with an exact partial result."""
        reference = _page_reference(page)
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
        _require_current_page(metadata)
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
        result = LabelChangeResult(page_id=metadata.page_id, unchanged=unchanged)
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

    async def _create_placement(
        self,
        *,
        parent_id: str | None,
        space_key: str | None,
        position: str,
        sibling: str | None,
    ) -> _Placement:
        if (parent_id is None) == (space_key is None):
            raise InvalidRequest("Pass exactly one of parent_id or space_key for page placement.")
        resolved_parent_id = None
        if parent_id is not None:
            resolved_parent = await self._resolve_page(_page_reference(parent_id))
            parent_metadata = await self._api.get_page_metadata(
                resolved_parent.page_id, expected_username=self._username
            )
            _require_current_page(parent_metadata)
            if parent_metadata.space_key is None:
                raise InvalidRequest("Confluence did not return the parent's space.")
            resolved_parent_id = parent_metadata.page_id
            selected_space = parent_metadata.space_key
        else:
            selected_space = _space_key(space_key)
        target_id: str | None = None
        if position in {"before", "after"}:
            if sibling is None:
                raise InvalidRequest("position before or after requires a sibling page reference.")
            target = await self._sibling(sibling)
            target_parent_id = target.ancestors[-1].page_id if target.ancestors else None
            if target_parent_id != resolved_parent_id or target.space_key != selected_space:
                raise InvalidRequest("The sibling must be in the same space and under the selected parent.")
            target_id = target.page_id
        elif sibling is not None:
            raise InvalidRequest("sibling is only valid with position before or after.")
        elif position == "first":
            target_id = await self._first_sibling(resolved_parent_id, selected_space)
        return _Placement(
            space_key=selected_space,
            parent_id=resolved_parent_id,
            position=position,
            target_id=target_id,
        )

    async def _first_sibling(self, parent_id: str | None, space_key: str) -> str | None:
        if parent_id is None:
            page = await self._api.list_space_root_pages(
                space_key, start=0, expected_username=self._username
            )
        else:
            page = await self._api.list_child_pages(
                parent_id, start=0, expected_username=self._username
            )
        if not page.hits:
            return None
        first_id = page.hits[0].content_id
        if first_id is None or not first_id.isdecimal():
            raise Upstream("Confluence did not return a page ID for the first sibling.")
        return first_id

    async def _sibling(self, reference: str) -> PageMetadata:
        """Resolve a page reference validated at the create_page public boundary."""
        resolved = await self._resolve_page(reference)
        metadata = await self._api.get_page_metadata(
            resolved.page_id, expected_username=self._username
        )
        _require_current_page(metadata)
        return metadata

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


def _page_reference(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 2_048
        or any(not char.isprintable() for char in value)
    ):
        raise InvalidRequest("Pass a non-empty page ID or supported same-instance page URL.")
    return value.strip()


def _title(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidRequest("title must be text.")
    result = value.strip()
    if not result or len(result) > MAX_TITLE_CHARS or any(not char.isprintable() for char in result):
        raise InvalidRequest(f"title must contain 1 to {MAX_TITLE_CHARS} printable characters.")
    return result


def _write_body(value: Any, page_format: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_WRITE_BODY_CHARS:
        raise InvalidRequest(f"body must be text with at most {MAX_WRITE_BODY_CHARS} characters.")
    return to_storage(value, page_format)


def _expected_version(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest("expected_version must be a positive integer.")
    return value


def _placement_kind(position: Any, sibling: Any) -> str:
    selected = "last" if position is None else position
    if not isinstance(selected, str) or selected not in {"first", "last", "before", "after"}:
        raise InvalidRequest("position must be first, last, before or after.")
    if sibling is not None and not isinstance(sibling, str):
        raise InvalidRequest("sibling must be a page ID or supported same-instance page URL.")
    if selected in {"before", "after"} and sibling is None:
        raise InvalidRequest("position before or after requires a sibling page reference.")
    if selected not in {"before", "after"} and sibling is not None:
        raise InvalidRequest("sibling is only valid with position before or after.")
    return selected


def _action_position(position: str) -> str | None:
    if position in {"before", "first"}:
        return "above"
    if position == "after":
        return "below"
    return None


def _space_key(value: Any) -> str:
    if not isinstance(value, str) or not _SPACE_KEY.fullmatch(value):
        raise InvalidRequest(
            "space_key must contain 1 to 255 letters, digits, underscores or hyphens, "
            "optionally prefixed by ~ for a personal space."
        )
    return value


def _optional_space_key(value: Any) -> str | None:
    return None if value is None else _space_key(value)


def _draft_limit(value: Any) -> int:
    result = 50 if value is None else value
    if not isinstance(result, int) or isinstance(result, bool) or not 1 <= result <= MAX_DRAFT_LIMIT:
        raise InvalidRequest(f"limit must be between 1 and {MAX_DRAFT_LIMIT}.")
    return result


def _decode_draft_cursor(value: str) -> tuple[str | None, int, int]:
    state = cursors.decode(value, _DRAFT_CURSOR_KIND)
    if set(state) != {"space_key", "start", "limit"}:
        raise InvalidRequest("Malformed draft cursor.")
    space_key = _optional_space_key(state.get("space_key"))
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


def _require_current_page(metadata: PageMetadata) -> None:
    if metadata.content_type != "page":
        raise InvalidRequest("This tool accepts Confluence pages only.")
    if metadata.status != "current":
        raise InvalidRequest("This tool accepts current published pages only.")


def _raise_account_error(error: ConnectorError) -> None:
    if isinstance(error, (AccountMismatch, AuthenticationRequired)):
        raise error
