"""Bounded page reads, parse-scope handling and process-independent block continuation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
)
from confluence_connector.domain.models import (
    PageAttachment,
    PageComment,
    PageParseScope,
    PageReadFailure,
    PageReadRecord,
    PageReadResult,
    PageReference,
    PageTextFormat,
    RemotePageComment,
    RemotePageContent,
    ResolvedPageReference,
)
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service import cursors
from confluence_connector.service.page_text import (
    PageTextBlock,
    html_to_text,
    render_page_body,
    storage_top_level_blocks,
)

MAX_PAGE_REFERENCES = 20
DEFAULT_MAX_CHARS = 40_000
MAX_MAX_CHARS = 400_000
MAX_BLOCK_OFFSET = 1_000_000
CURSOR_KIND = "get_pages"


@dataclass(frozen=True)
class _PageRequest:
    reference: str
    page_id: str
    version: int | None


@dataclass(frozen=True)
class _LoadedPage:
    source: RemotePageContent
    extracted_at: str
    attachments: tuple[PageAttachment, ...]
    comments: tuple[PageComment, ...]
    blocks: tuple[PageTextBlock, ...]
    references: tuple[PageReference, ...]
    header: str | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _CursorState:
    current: _PageRequest
    version: int
    offset: int
    pending: tuple[_PageRequest, ...]
    format: PageTextFormat
    max_chars: int
    parse_scope: PageParseScope


class PageTextService:
    """Read page text or storage bodies from validated IDs and same-instance page links."""

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str, base_url: str) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._base_url = base_url

    async def get_pages(
        self,
        pages: str | list[str] | None = None,
        *,
        format: PageTextFormat | None = None,
        max_chars: int | None = None,
        parse_scope: PageParseScope | dict[str, Any] | None = None,
        cursor: str | None = None,
    ) -> PageReadResult:
        """Return pages within a character budget, cutting an oversized first page at block edges.

        Entry-point validation covers references, format, budget, parse scope and cursor state. Internal
        readers trust numeric page IDs and positive versions after resolution or cursor validation.
        """
        if cursor is not None:
            if any(value is not None for value in (pages, format, max_chars, parse_scope)):
                raise InvalidRequest("Pass only the cursor; it already contains the page request and scope.")
            state = _decode_cursor(cursor)
            return await self._continue(state)

        references = _page_references(pages)
        page_format = _format(format)
        budget = _character_budget(max_chars)
        scope = _parse_scope(parse_scope)
        requests, failures = await self._resolve(references)
        return await self._read_requests(
            requests,
            failures=failures,
            page_format=page_format,
            max_chars=budget,
            parse_scope=scope,
        )

    async def _resolve(
        self, references: list[str]
    ) -> tuple[list[_PageRequest], list[PageReadFailure]]:
        async def resolve(reference: str) -> tuple[_PageRequest | None, PageReadFailure | None]:
            try:
                resolved: ResolvedPageReference = await self._links.resolve(
                    reference, expected_username=self._username
                )
                if not resolved.page_id.isdecimal():
                    raise InvalidRequest("The selected reference did not resolve to a numeric page ID.")
                return _PageRequest(reference, resolved.page_id, resolved.version), None
            except ConnectorError as exc:
                _raise_account_error(exc)
                return None, PageReadFailure(reference=reference, error=f"{type(exc).__name__}: {exc}")

        resolved = await asyncio.gather(*(resolve(reference) for reference in references))
        return (
            [request for request, _ in resolved if request is not None],
            [failure for _, failure in resolved if failure is not None],
        )

    async def _continue(self, state: _CursorState) -> PageReadResult:
        current = _PageRequest(state.current.reference, state.current.page_id, state.version)
        return await self._read_requests(
            [current, *state.pending],
            failures=[],
            page_format=state.format,
            max_chars=state.max_chars,
            parse_scope=state.parse_scope,
            first_offset=state.offset,
            pinned_first_version=state.version,
        )

    async def _read_requests(
        self,
        requests: list[_PageRequest],
        *,
        failures: list[PageReadFailure],
        page_format: PageTextFormat,
        max_chars: int,
        parse_scope: PageParseScope,
        first_offset: int = 0,
        pinned_first_version: int | None = None,
    ) -> PageReadResult:
        records: list[PageReadRecord] = []
        notes: list[str] = []
        used_chars = 0
        pending: dict[int, asyncio.Task[_LoadedPage]] = {}

        def schedule(index: int) -> None:
            if index >= len(requests):
                return
            request = requests[index]
            pending[index] = asyncio.create_task(
                self._load_page(
                    request,
                    page_format=page_format,
                    parse_scope=parse_scope,
                    forced_version=pinned_first_version if index == 0 else None,
                )
            )

        schedule(0)
        schedule(1)
        try:
            for index, request in enumerate(requests):
                try:
                    loaded = await pending.pop(index)
                except ConnectorError as exc:
                    _raise_account_error(exc)
                    failures.append(PageReadFailure(reference=request.reference, error=f"{type(exc).__name__}: {exc}"))
                    schedule(index + 2)
                    continue

                start_offset = first_offset if index == 0 else 0
                selected, next_offset, chars, exceeded = _take_blocks(
                    loaded.blocks,
                    start=start_offset,
                    budget=max_chars - used_chars,
                    page_format=page_format,
                )
                if used_chars > 0 and (next_offset is not None or exceeded):
                    return PageReadResult(
                        pages=records,
                        remaining=[item.reference for item in requests[index:]],
                        failures=failures,
                        notes=notes,
                    )

                records.append(_record(loaded, selected, page_format=page_format))
                used_chars += chars
                notes.extend(loaded.notes)
                if exceeded:
                    notes.append(
                        "An indivisible first block exceeds max_chars; it was returned whole to preserve structure."
                    )
                if next_offset is not None:
                    remaining = requests[index + 1 :]
                    cursor = cursors.encode(
                        CURSOR_KIND,
                        page_id=request.page_id,
                        reference=request.page_id,
                        version=loaded.source.version,
                        offset=next_offset,
                        pending=[_request_json(item) for item in remaining],
                        format=page_format,
                        max_chars=max_chars,
                        parse_scope=parse_scope.model_dump(mode="json"),
                    )
                    return PageReadResult(
                        pages=records,
                        remaining=[request.page_id, *(item.reference for item in remaining)],
                        failures=failures,
                        next_cursor=cursor,
                        notes=notes,
                    )
                if used_chars >= max_chars and index + 1 < len(requests):
                    return PageReadResult(
                        pages=records,
                        remaining=[item.reference for item in requests[index + 1 :]],
                        failures=failures,
                        notes=notes,
                    )
                schedule(index + 2)
            return PageReadResult(pages=records, failures=failures, notes=notes)
        finally:
            unfinished = list(pending.values())
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

    async def _load_page(
        self,
        request: _PageRequest,
        *,
        page_format: PageTextFormat,
        parse_scope: PageParseScope,
        forced_version: int | None,
    ) -> _LoadedPage:
        version = forced_version if forced_version is not None else request.version
        include_storage = page_format == "rich" or parse_scope.macros_from_storage
        tasks: list[Any] = [
            self._api.get_page_content(
                request.page_id,
                version=version,
                include_storage=include_storage,
                expected_username=self._username,
            )
        ]
        task_names = ["body"]
        if parse_scope.attachments:
            tasks.append(self._api.list_page_attachments(request.page_id, expected_username=self._username))
            task_names.append("attachments")
        if parse_scope.comments:
            tasks.append(self._api.list_page_comments(request.page_id, expected_username=self._username))
            task_names.append("comments")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        source = results[0]
        if isinstance(source, BaseException):
            raise source
        if not isinstance(source, RemotePageContent):
            raise InvalidRequest("Confluence returned an unexpected page content record.")

        attachments: list[PageAttachment] = []
        remote_comments: list[RemotePageComment] = []
        notes: list[str] = []
        for name, result in zip(task_names[1:], results[1:], strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, ConnectorError):
                    _raise_account_error(result)
                    notes.append(f"The {name} section could not be read ({type(result).__name__}).")
                    continue
                raise result
            if name == "attachments":
                attachments = result
                if len(attachments) == 500:
                    notes.append("The attachment list reached its 500-item request limit and may be incomplete.")
            else:
                remote_comments = result
                if len(remote_comments) == 500:
                    notes.append("The comment list reached its 500-item request limit and may be incomplete.")

        converted = render_page_body(
            source.view_body,
            source.storage_body,
            page_id=request.page_id,
            attachments=attachments,
            base_url=self._base_url,
            macros_from_storage=parse_scope.macros_from_storage,
        )
        comments = tuple(
            PageComment(
                comment_id=comment.comment_id,
                parent_comment_id=comment.parent_comment_id,
                author=comment.author,
                created_at=comment.created_at,
                body=html_to_text(comment.body_view),
                location=comment.location,
                resolution=comment.resolution,
                selected_text=comment.selected_text,
            )
            for comment in remote_comments
        )
        references = list(converted.references)
        references.extend(_attachment_reference(item, request.page_id) for item in attachments)
        references.extend(_comment_reference(item, request.page_id) for item in comments)
        extracted_at = _utc_now()
        header = _page_header(source, extracted_at) if parse_scope.header else None
        blocks = _page_blocks(
            page_format,
            source,
            converted.blocks,
            attachments,
            comments,
            parse_scope,
            header,
        )
        return _LoadedPage(
            source=source,
            extracted_at=extracted_at,
            attachments=tuple(attachments),
            comments=comments,
            blocks=tuple(blocks),
            references=tuple(_unique_references(references)),
            header=header,
            notes=tuple(notes),
        )


def _page_references(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise InvalidRequest("pages must be one page reference or a list of page references.")
    if not values or len(values) > MAX_PAGE_REFERENCES:
        raise InvalidRequest(f"pages must contain 1 to {MAX_PAGE_REFERENCES} page references.")
    result: list[str] = []
    for item in values:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item.strip()) > 2_048
            or any(ord(character) < 32 for character in item)
        ):
            raise InvalidRequest("pages contains an invalid page reference.")
        result.append(item.strip())
    return result


def _parse_scope(value: PageParseScope | dict[str, Any] | None) -> PageParseScope:
    try:
        return PageParseScope() if value is None else PageParseScope.model_validate(value)
    except ValidationError as exc:
        raise InvalidRequest(f"Invalid page parse scope: {exc.errors()[0]['msg']}.") from None


def _format(value: Any) -> PageTextFormat:
    if value is None:
        return "text"
    if not isinstance(value, str) or value not in {"text", "rich"}:
        raise InvalidRequest("format must be text or rich.")
    return value


def _character_budget(value: Any) -> int:
    if value is None:
        return DEFAULT_MAX_CHARS
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_MAX_CHARS:
        raise InvalidRequest(f"max_chars must be from 1 to {MAX_MAX_CHARS}.")
    return value


def _page_header(source: RemotePageContent, extracted_at: str) -> str:
    metadata = source.metadata
    lines = [f"# {metadata.title}"]
    if metadata.url:
        lines.append(f"Link: {metadata.url}")
    version = f"Version: {source.version}"
    if source.total_versions is not None:
        version += f" of {source.total_versions}"
    lines.append(version)
    if metadata.modified_at:
        lines.append(f"Last modified: {metadata.modified_at}")
    lines.append(f"Extracted at: {extracted_at}")
    return "\n".join(lines)


def _page_blocks(
    page_format: PageTextFormat,
    source: RemotePageContent,
    text_blocks: tuple[PageTextBlock, ...],
    attachments: list[PageAttachment],
    comments: tuple[PageComment, ...],
    scope: PageParseScope,
    header: str | None,
) -> list[PageTextBlock]:
    if page_format == "rich":
        blocks = [PageTextBlock(value) for value in storage_top_level_blocks(source.storage_body)]
        if scope.comments:
            blocks.extend(_comment_blocks(comments, source.metadata.page_id))
        return blocks
    blocks: list[PageTextBlock] = []
    if header:
        blocks.append(PageTextBlock(header))
    blocks.extend(text_blocks)
    if scope.attachments:
        blocks.append(PageTextBlock("## Attachments"))
        for attachment in attachments:
            details = [attachment.media_type or "unknown type"]
            if attachment.file_size is not None:
                details.append(f"{attachment.file_size} bytes")
            line = f"- {attachment.filename} ({', '.join(details)})"
            blocks.append(_block(line, (_attachment_reference(attachment, source.metadata.page_id),)))
    if scope.comments:
        blocks.append(PageTextBlock("## Comments"))
        blocks.extend(_comment_blocks(comments, source.metadata.page_id))
    return blocks


def _comment_blocks(comments: tuple[PageComment, ...], page_id: str) -> list[PageTextBlock]:
    by_id = {comment.comment_id: comment for comment in comments}
    blocks: list[PageTextBlock] = []
    for comment in comments:
        depth = 0
        parent_id = comment.parent_comment_id
        visited = {comment.comment_id}
        while parent_id and parent_id in by_id and parent_id not in visited and depth < 20:
            visited.add(parent_id)
            depth += 1
            parent_id = by_id[parent_id].parent_comment_id
        author = comment.author.display_name if comment.author else None
        author = author or (comment.author.username if comment.author else None) or "Unknown author"
        details = [f"@{author}"]
        if comment.created_at:
            details.append(comment.created_at)
        if comment.location:
            details.append(comment.location)
        if comment.resolution:
            details.append(comment.resolution)
        lines = ["  " * depth + "- " + " — ".join(details)]
        if comment.selected_text:
            lines.append("  " * (depth + 1) + f'Selected text: "{comment.selected_text}"')
        if comment.body:
            lines.extend("  " * (depth + 1) + line for line in comment.body.splitlines())
        blocks.append(
            _block(
                "\n".join(lines),
                (_comment_reference(comment, page_id),),
            )
        )
    return blocks


def _record(
    loaded: _LoadedPage,
    blocks: list[PageTextBlock],
    *,
    page_format: PageTextFormat,
) -> PageReadRecord:
    selected_comment_ids = {
        reference.comment_id
        for block in blocks
        for reference in block.references
        if reference.kind == "comment" and reference.comment_id is not None
    }
    if page_format == "rich":
        text = "".join(
            block.text
            for block in blocks
            if not any(reference.kind == "comment" for reference in block.references)
        )
    else:
        text = "\n\n".join(block.text for block in blocks)
    metadata = loaded.source.metadata
    return PageReadRecord(
        page_id=metadata.page_id,
        title=metadata.title,
        url=metadata.url,
        version=loaded.source.version,
        total_versions=loaded.source.total_versions,
        modified_at=metadata.modified_at,
        extracted_at=loaded.extracted_at,
        format=page_format,
        text=text,
        header=loaded.header,
        references=list(loaded.references),
        attachments=list(loaded.attachments),
        comments=[comment for comment in loaded.comments if comment.comment_id in selected_comment_ids],
        notes=list(loaded.notes),
    )


def _take_blocks(
    blocks: tuple[PageTextBlock, ...], *, start: int, budget: int, page_format: PageTextFormat
) -> tuple[list[PageTextBlock], int | None, int, bool]:
    selected: list[PageTextBlock] = []
    used = 0
    separator = "" if page_format == "rich" else "\n\n"
    for index in range(start, len(blocks)):
        block = blocks[index]
        addition = len(block.text) + (len(separator) if selected else 0)
        if used + addition > budget:
            if not selected:
                selected.append(block)
                used += len(block.text)
                return selected, index + 1 if index + 1 < len(blocks) else None, used, True
            return selected, index, used, False
        selected.append(block)
        used += addition
    return selected, None, used, False


def _decode_cursor(cursor: str) -> _CursorState:
    state = cursors.decode(cursor, CURSOR_KIND)
    page_id = state.get("page_id")
    reference = state.get("reference")
    version = state.get("version")
    offset = state.get("offset")
    pending_value = state.get("pending")
    page_format = state.get("format")
    budget = state.get("max_chars")
    parse_scope = state.get("parse_scope")
    if (
        not isinstance(page_id, str)
        or not page_id.isdecimal()
        or not isinstance(reference, str)
        or not reference
        or not reference.isdecimal()
        or reference != page_id
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or not 1 <= offset <= MAX_BLOCK_OFFSET
        or not isinstance(pending_value, list)
        or len(pending_value) > MAX_PAGE_REFERENCES - 1
        or not isinstance(page_format, str)
        or page_format not in {"text", "rich"}
        or not isinstance(budget, int)
        or isinstance(budget, bool)
        or not 1 <= budget <= MAX_MAX_CHARS
    ):
        raise InvalidRequest("Malformed get_pages cursor state.")
    try:
        pending = tuple(_request_from_json(item) for item in pending_value)
        scope = PageParseScope.model_validate(parse_scope)
    except (ValidationError, InvalidRequest):
        raise InvalidRequest("Malformed get_pages cursor state.") from None
    return _CursorState(
        current=_PageRequest(reference, page_id, version),
        version=version,
        offset=offset,
        pending=pending,
        format=page_format,
        max_chars=budget,
        parse_scope=scope,
    )


def _request_json(request: _PageRequest) -> dict[str, Any]:
    return {"reference": request.page_id, "page_id": request.page_id, "version": request.version}


def _request_from_json(value: Any) -> _PageRequest:
    if not isinstance(value, dict):
        raise InvalidRequest("Malformed page reference in cursor.")
    reference = value.get("reference")
    page_id = value.get("page_id")
    version = value.get("version")
    if (
        not isinstance(reference, str)
        or not reference
        or not reference.isdecimal()
        or len(reference) > 2_048
        or not isinstance(page_id, str)
        or not page_id.isdecimal()
        or reference != page_id
        or (version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 1))
    ):
        raise InvalidRequest("Malformed page reference in cursor.")
    return _PageRequest(reference, page_id, version)


def _attachment_reference(attachment: PageAttachment, page_id: str) -> PageReference:
    return PageReference(
        kind="attachment",
        attachment_id=attachment.attachment_id,
        filename=attachment.filename,
        media_type=attachment.media_type,
        size=attachment.file_size,
        owner_page_id=page_id,
        url=attachment.download_url,
    )


def _comment_reference(comment: PageComment, page_id: str) -> PageReference:
    return PageReference(kind="comment", comment_id=comment.comment_id, owner_page_id=page_id)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _block(text: str, references: tuple[PageReference, ...]) -> PageTextBlock:
    return PageTextBlock(text=text, references=references)


def _unique_references(references: list[PageReference]) -> list[PageReference]:
    unique: list[PageReference] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for reference in references:
        key = tuple(sorted((name, str(value)) for name, value in reference.model_dump(exclude_none=True).items()))
        if key not in seen:
            seen.add(key)
            unique.append(reference)
    return unique


def _raise_account_error(error: ConnectorError) -> None:
    if isinstance(error, (AuthenticationRequired, AccountMismatch)):
        raise error
