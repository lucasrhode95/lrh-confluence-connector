"""Read and manage footer and inline comments on current pages."""

from __future__ import annotations

from confluence_connector.domain.errors import Conflict, InvalidRequest
from confluence_connector.domain.models import (
    CommentDeletionResult,
    CommentRecord,
    CommentWriteResult,
    PageMetadata,
    RemotePageComment,
)
from confluence_connector.domain.validation import (
    NOTIFICATION_SUPPRESSION_UNAVAILABLE,
    page_reference,
    require_current_page,
    validate_notify_watchers,
)
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service.page_text import render_page_body
from confluence_connector.service.write_markup import to_storage

MAX_COMMENT_BODY_CHARS = 2_000_000


class CommentManagement:
    """Validate comment ownership, versions and content before account-bound writes."""

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str, base_url: str) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._base_url = base_url.rstrip("/")

    async def get_comment(self, comment_id: str) -> CommentRecord:
        selected_id = _comment_id(comment_id)
        remote = await self._api.get_comment(selected_id, expected_username=self._username)
        metadata = await self._api.get_page_metadata(
            remote.container_page_id or "", expected_username=self._username
        )
        return self._record(remote, metadata)

    async def add_comment(
        self,
        page: str,
        body: str,
        *,
        format: str = "rich",
        parent_comment_id: str | None = None,
        notify_watchers: bool = True,
    ) -> CommentWriteResult:
        notify = validate_notify_watchers(notify_watchers)
        storage_body = _comment_body(body, format)
        metadata = await self._resolve_current_page(page)
        parent_id = _comment_id(parent_comment_id) if parent_comment_id is not None else None
        if parent_id is not None:
            parent = await self._api.get_comment(parent_id, expected_username=self._username)
            if parent.container_page_id != metadata.page_id:
                raise InvalidRequest("The parent comment must belong to the named page.")
        comment_id = await self._api.create_comment(
            metadata.page_id,
            storage_body,
            expected_username=self._username,
            parent_comment_id=parent_id,
        )
        record = CommentRecord(
            comment_id=comment_id,
            page_id=metadata.page_id,
            page_url=metadata.url,
            version=1,
            parent_comment_id=parent_id,
            body=_render_body(storage_body, metadata.page_id, self._base_url),
        )
        return CommentWriteResult(
            comment=record,
            notify_watchers=notify,
            notification_suppression_supported=False,
            notification_note=NOTIFICATION_SUPPRESSION_UNAVAILABLE,
        )

    async def update_comment(
        self,
        comment_id: str,
        body: str,
        *,
        expected_version: int,
        format: str = "rich",
        page: str | None = None,
        notify_watchers: bool = True,
    ) -> CommentWriteResult:
        selected_id = _comment_id(comment_id)
        version = _expected_version(expected_version)
        notify = validate_notify_watchers(notify_watchers)
        storage_body = _comment_body(body, format)
        current = await self._api.get_comment(selected_id, expected_username=self._username)
        metadata = await self._api.get_page_metadata(
            current.container_page_id or "", expected_username=self._username
        )
        if page is not None:
            selected_page = await self._resolve_current_page(page)
            if selected_page.page_id != metadata.page_id:
                raise InvalidRequest("The comment does not belong to the named page.")
        if current.version != version:
            raise Conflict(
                f"The comment is now version {current.version}; expected_version was {version}. "
                "Read it again and reapply the changes."
            )
        await self._api.update_comment(
            selected_id,
            metadata.page_id,
            storage_body,
            next_version=version + 1,
            expected_username=self._username,
            notify_watchers=notify,
        )
        updated = current.model_copy(
            update={"version": version + 1, "body_view": storage_body, "body_storage": storage_body}
        )
        return CommentWriteResult(
            comment=self._record(updated, metadata),
            notify_watchers=notify,
            notification_suppression_supported=True,
        )

    async def delete_comment(
        self,
        comment_id: str,
        *,
        confirm_permanent_delete: bool,
        page: str | None = None,
        notify_watchers: bool = True,
    ) -> CommentDeletionResult:
        selected_id = _comment_id(comment_id)
        if not isinstance(confirm_permanent_delete, bool) or not confirm_permanent_delete:
            raise InvalidRequest("Pass confirm_permanent_delete=true to permanently delete this comment.")
        notify = validate_notify_watchers(notify_watchers)
        remote = await self._api.get_comment(selected_id, expected_username=self._username)
        metadata = await self._api.get_page_metadata(
            remote.container_page_id or "", expected_username=self._username
        )
        if page is not None:
            selected_page = await self._resolve_current_page(page)
            if selected_page.page_id != metadata.page_id:
                raise InvalidRequest("The comment does not belong to the named page.")
        await self._api.delete_comment(selected_id, expected_username=self._username)
        return CommentDeletionResult(
            comment_id=selected_id,
            page_id=metadata.page_id,
            page_url=metadata.url,
            deleted=True,
            permanent=True,
            notify_watchers=notify,
            notification_suppression_supported=False,
            notification_note=NOTIFICATION_SUPPRESSION_UNAVAILABLE,
        )

    async def _resolve_current_page(self, reference: str) -> PageMetadata:
        clean_reference = page_reference(reference)
        resolved = await self._links.resolve(clean_reference, expected_username=self._username)
        if resolved.version is not None:
            raise InvalidRequest("Comment writes target the current page; omit pageVersion from the reference.")
        if not resolved.page_id.isdecimal():
            raise InvalidRequest("The selected page reference did not resolve to a numeric page ID.")
        metadata = await self._api.get_page_metadata(
            resolved.page_id, expected_username=self._username
        )
        require_current_page(metadata)
        return metadata

    def _record(self, comment: RemotePageComment, page: PageMetadata) -> CommentRecord:
        rendered = _render_body(comment.body_view, page.page_id, self._base_url)
        return CommentRecord(
            comment_id=comment.comment_id,
            page_id=page.page_id,
            page_url=page.url,
            version=comment.version or 1,
            parent_comment_id=comment.parent_comment_id,
            author=comment.author,
            created_at=comment.created_at,
            body=rendered,
            location=comment.location,
            resolution=comment.resolution,
            selected_text=comment.selected_text,
        )


def _comment_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip().isdecimal():
        raise InvalidRequest("comment_id must be a decimal identifier.")
    return value.strip()


def _expected_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidRequest("expected_version must be a positive integer.")
    return value


def _comment_body(value: object, page_format: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_COMMENT_BODY_CHARS:
        raise InvalidRequest(f"body must be text with at most {MAX_COMMENT_BODY_CHARS} characters.")
    if not isinstance(page_format, str) or page_format not in {"rich", "text"}:
        raise InvalidRequest("format must be rich or text.")
    return to_storage(value, page_format)


def _render_body(body: str, page_id: str, base_url: str) -> str:
    rendered = render_page_body(
        body,
        "",
        page_id=page_id,
        attachments=[],
        base_url=base_url,
        macros_from_storage=False,
    )
    return "".join(
        (block.separator_before if index else "") + block.text
        for index, block in enumerate(rendered.blocks)
    )
