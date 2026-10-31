"""Read and manage footer and inline comments on current pages."""

from __future__ import annotations

from confluence_connector.domain.errors import Conflict, ConnectorError, InvalidRequest
from confluence_connector.domain.models import (
    CommentDeletionResult,
    CommentRecord,
    CommentWriteResult,
    PageMetadata,
    RemotePageComment,
)
from confluence_connector.domain.validation import (
    notification_outcome,
    page_reference,
    require_current_page,
    validate_notify_watchers,
)
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service.page_text import render_page_body
from confluence_connector.service.write_markup import body_to_storage


class CommentManagement:
    """Validate comment ownership, versions and content before account-bound writes.

    Public methods validate IDs, versions, bodies and flags before any request. The comment read
    supplies its page ID and URL, so ownership checks compare resolved IDs without a metadata read.
    """

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str, base_url: str) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._base_url = base_url.rstrip("/")

    async def get_comment(self, comment_id: str) -> CommentRecord:
        remote = await self._api.get_comment(_comment_id(comment_id), expected_username=self._username)
        return self._record(remote)

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
        storage_body = body_to_storage(body, format)
        parent_id = _comment_id(parent_comment_id) if parent_comment_id is not None else None
        metadata = await self._resolve_current_page(page)
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
        saved = CommentRecord(
            comment_id=comment_id,
            page_id=metadata.page_id,
            page_url=metadata.url,
            version=1,
            parent_comment_id=parent_id,
        )
        return await self._write_result(saved, notify, supported=False)

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
        """Replace the body only; inline anchor metadata is not resent with the update."""
        selected_id = _comment_id(comment_id)
        version = _expected_version(expected_version)
        notify = validate_notify_watchers(notify_watchers)
        storage_body = body_to_storage(body, format)
        current = await self._api.get_comment(selected_id, expected_username=self._username)
        if page is not None:
            await self._require_owner(page, current)
        if current.version != version:
            raise Conflict(
                f"The comment is now version {current.version}; expected_version was {version}. "
                "Read it again and reapply the changes."
            )
        await self._api.update_comment(
            selected_id,
            current.container_page_id,
            storage_body,
            next_version=version + 1,
            expected_username=self._username,
            notify_watchers=notify,
        )
        saved = self._record(current).model_copy(update={"version": version + 1, "body": ""})
        return await self._write_result(saved, notify, supported=True)

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
        if page is not None:
            await self._require_owner(page, remote)
        await self._api.delete_comment(selected_id, expected_username=self._username)
        return CommentDeletionResult(
            comment_id=selected_id,
            page_id=remote.container_page_id,
            page_url=remote.container_page_url,
            deleted=True,
            permanent=True,
            **notification_outcome(notify, supported=False),
        )

    async def _write_result(self, saved: CommentRecord, notify: bool, *, supported: bool) -> CommentWriteResult:
        """Reread a confirmed write for its stored body; a failed reread keeps the write's known facts."""
        warnings: list[str] = []
        try:
            record = self._record(
                await self._api.get_comment(saved.comment_id, expected_username=self._username)
            )
        except ConnectorError as exc:
            record = saved
            warnings.append(
                f"The comment was saved, but it could not be read back ({type(exc).__name__}); the body is omitted."
            )
        return CommentWriteResult(
            comment=record, warnings=warnings, **notification_outcome(notify, supported=supported)
        )

    async def _resolve_current_page(self, reference: str) -> PageMetadata:
        metadata = await self._api.get_page_metadata(
            await self._resolve_page_id(reference), expected_username=self._username
        )
        require_current_page(metadata)
        return metadata

    async def _require_owner(self, reference: str, comment: RemotePageComment) -> None:
        if await self._resolve_page_id(reference) != comment.container_page_id:
            raise InvalidRequest("The comment does not belong to the named page.")

    async def _resolve_page_id(self, reference: str) -> str:
        resolved = await self._links.resolve(page_reference(reference), expected_username=self._username)
        if resolved.version is not None:
            raise InvalidRequest("Comment writes target the current page; omit pageVersion from the reference.")
        if not resolved.page_id.isdecimal():
            raise InvalidRequest("The selected page reference did not resolve to a numeric page ID.")
        return resolved.page_id

    def _record(self, comment: RemotePageComment) -> CommentRecord:
        """Map a comment read; the adapter guarantees its page ID and version."""
        return CommentRecord(
            comment_id=comment.comment_id,
            page_id=comment.container_page_id,
            page_url=comment.container_page_url,
            version=comment.version,
            parent_comment_id=comment.parent_comment_id,
            author=comment.author,
            created_at=comment.created_at,
            body=_render_body(comment.body_view, comment.container_page_id, self._base_url),
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
