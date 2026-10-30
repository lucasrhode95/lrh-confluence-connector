"""MCP declarations for account-bound Confluence reads, writes and local-file outputs."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from urllib.request import url2pathname

from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ClientCapabilities, RootsCapability, ToolAnnotations
from pydantic import Field
from confluence_connector import config
from confluence_connector.bootstrap import AppContext
from confluence_connector.domain.models import (
    AuthStatus,
    PageReadResult,
    PageMetadataResult,
    PageTreeResult,
    UserSearchResult,
    ContentSearchResult,
    AttachmentDownloadResult,
    AttachmentMutationResult,
    DraftDeletionResult,
    DraftListResult,
    LabelChangeResult,
    PageExportResult,
    PageParseScope,
    PageDeleteResult,
    PageMoveResult,
    PageRestoreResult,
    PageTextFormat,
    PageWriteResult,
    SearchFields,
    SearchDetail,
    SearchMatch,
    SearchOrderBy,
    SearchScope,
    SpaceList,
    TrashListResult,
)

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
OFFLINE_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
# Replacement/deletion and local output cleanup can remove existing data.
DESTRUCTIVE_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

INSTRUCTIONS = """\
Confluence Data Center access for one signed-in local account and the configured instance only.
Read tools return typed source references; links to other products remain links. Attachment bytes
are fetched only by explicit download/export options. Incomplete coverage is reported in results.
Search can list pages without keywords when a narrowing scope is supplied; use find_users to resolve
display names, contributor means anyone who edited content, and creator means its original author.
Continue with the returned cursor alone; it contains the original search, scope, ordering and detail.
Write tools require the user's explicit request. Never retry an uncertain write before inspecting
its target. Recreating a page after a refused move is a separate recovery plan: it loses its ID,
history and comments. Authenticate through `confluence-connector auth`, never through an MCP tool;
`auth_status` is offline and does not establish token validity.
To edit a page, export it with `export_pages` (`format: "rich"`), change its body file with exact
find-and-replace, then pass that body file to `update_page` as `body_file` with the same `page`.
Comments are in a separate read-only file. Never retype a large body inline or rebuild it from text
output.
"""


def build_server(context: AppContext) -> FastMCP:
    """Build the MCP surface around one process-scoped application context."""
    mcp = FastMCP(config.MCP_SERVER_NAME, instructions=INSTRUCTIONS, log_level="WARNING")

    @mcp.tool(annotations=OFFLINE_READ)
    @_logged
    async def auth_status() -> AuthStatus:
        """Report the local token and account status without contacting Confluence."""
        status: AuthStatus = await context.auth_status()
        return status

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def list_spaces() -> SpaceList:
        """List spaces visible to the stored account; pages internally in batches of up to 500."""
        services = await context.services()
        spaces: SpaceList = await services.spaces.list_spaces()
        return spaces

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def search_content(
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Literal search text. Omit it to list pages by a narrowing scope such as "
                    "contributor_usernames, creator_usernames, date bounds, spaces, labels, parent "
                    "or ancestor. Use find_users to resolve display names. contributor means anyone "
                    "who edited the content; creator means its original author."
                )
            ),
        ] = None,
        scope: Annotated[
            SearchScope | None,
            Field(
                description="Structured filters for spaces, types, labels, users, dates and page ancestry."
            ),
        ] = None,
        match: Annotated[
            SearchMatch | None,
            Field(description="Term matching: all (default), any, or phrase."),
        ] = None,
        fields: Annotated[
            SearchFields | None,
            Field(description="Search all supported fields (default) or title only."),
        ] = None,
        order_by: Annotated[
            SearchOrderBy | None,
            Field(
                description=(
                    "Sort by relevance (default with text), modified_desc (default without text), "
                    "modified_asc or created_desc."
                )
            ),
        ] = None,
        detail: Annotated[
            SearchDetail | None,
            Field(description="Return compact hit fields only, or full details (default)."),
        ] = None,
        limit: Annotated[
            int | None,
            Field(ge=1, le=100, description="Results per page, 1 through 100 (default 25)."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Continuation cursor from an earlier result. Pass it alone."),
        ] = None,
    ) -> ContentSearchResult:
        """Search Confluence using literal text and structured scope filters.

        For continuation, pass only the returned cursor. `match` is all, any or phrase; `fields`
        is all or title. Scope supports spaces, space type, content types, labels, creators,
        contributors, created/modified ranges, parent/ancestor and include_archived. Date-only values
        mean a calendar day in Confluence's timezone, and mixed bounds are checked against that full
        day. ISO-8601 timestamps with offsets are accepted;
        timestamps without offsets are treated as UTC. Timestamp filters require cql_timezone; run
        `confluence-connector status --check` if it is unset. Quick-navigation supplements apply only
        to unscoped title searches. Wildcards are not query operators; omit query and use scope to
        list pages. Queryless searches default to pages ordered by modified time. Responses are kept
        under the configured character budget, with smaller cursor pages when needed. Use detail='compact'
        to omit excerpts and ancestor lists. Common English stopword-only queries may return no matches;
        omit query to list by scope.
        """
        services = await context.services()
        result = await services.search.search_content(
            query,
            scope=scope,
            match=match,
            fields=fields,
            order_by=order_by,
            detail=detail,
            limit=limit,
            cursor=cursor,
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def find_users(
        query: Annotated[
            str | None,
            Field(description="Display-name search for current users; omit with username or cursor."),
        ] = None,
        username: Annotated[
            str | None,
            Field(description="Exact username lookup, including former users; use alone."),
        ] = None,
        limit: Annotated[
            int | None,
            Field(ge=1, le=100, description="Results per page, 1 through 100 (default 25)."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Continuation cursor for a display-name search. Pass it alone."),
        ] = None,
    ) -> UserSearchResult:
        """Find current users by display-name query, or look up one exact username.

        Former users are available only by exact username. Pass only cursor to continue a name search.
        """
        services = await context.services()
        result = await services.users.find_users(
            query,
            username=username,
            limit=limit,
            cursor=cursor,
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_page_tree(
        page: Annotated[
            str | None,
            Field(description="Numeric page ID or supported URL; use exactly one of page and space_key."),
        ] = None,
        space_key: Annotated[
            str | None,
            Field(description="Space key for a space-root tree; use exactly one of page and space_key."),
        ] = None,
        max_depth: Annotated[
            int | None,
            Field(ge=1, le=5, description="Levels below the root to include, 1 through 5 (default 3)."),
        ] = None,
    ) -> PageTreeResult:
        """Return ordered child pages from exactly one page reference or space root.

        Page references may be numeric IDs or supported same-instance Confluence URLs. max_depth
        counts levels below the root, for a page or a space alike: 1 lists direct children (or a
        space's top-level pages), 1 through 5, default 3. At most 1,000 pages; when the cap is
        reached, shallower levels are complete and complete is false.
        """
        services = await context.services()
        result = await services.tree.get_page_tree(
            page=page,
            space_key=space_key,
            max_depth=max_depth,
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_page_metadata(
        pages: Annotated[
            list[str] | str,
            Field(description="One to 20 numeric page IDs or supported same-instance page URLs."),
        ]
    ) -> PageMetadataResult:
        """Return history, versions, labels, contributors and ancestors for up to 20 page references."""
        services = await context.services()
        result = await services.metadata.get_page_metadata(pages)
        return result

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_pages(
        pages: Annotated[
            list[str] | str | None,
            Field(
                description="One to 20 numeric page IDs or supported URLs; omit when continuing with cursor."
            ),
        ] = None,
        format: Annotated[
            PageTextFormat | None,
            Field(description="text (default) or unchanged rich storage markup."),
        ] = None,
        max_chars: Annotated[
            int | None,
            Field(ge=1, le=400_000, description="Character budget from 1 to 400000 (default 40000)."),
        ] = None,
        parse_scope: Annotated[
            PageParseScope | None,
            Field(description="Choose header, macro/reference parsing, attachment lists and comments."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Version-pinned continuation cursor. Pass it alone."),
        ] = None,
    ) -> PageReadResult:
        """Read up to 20 page IDs or same-instance URLs as text or rich storage.

        Text is the default. Rich returns storage body markup unchanged. The default parse_scope
        includes header, storage macro identities, attachment lists and threaded comments. max_chars
        defaults to 40000 and is capped at 400000. Comment bodies share the block budget, including
        in rich format, and structured comment records accompany selected blocks only. Whole pages
        are packed first; an oversized first page is returned at block boundaries with a version-pinned
        cursor. Pass only that cursor to continue. Attachments are listed, never downloaded.
        """
        services = await context.services()
        result = await services.pages.get_pages(
            pages,
            format=format,
            max_chars=max_chars,
            parse_scope=parse_scope,
            cursor=cursor,
        )
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def download_attachments(
        page: Annotated[str, Field(description="Page ID or supported same-instance page URL.")],
        attachment_ids: Annotated[
            list[str],
            Field(
                min_length=1,
                max_length=500,
                description="One to 500 attachment IDs belonging to the page.",
            ),
        ],
    ) -> AttachmentDownloadResult:
        """Save selected attachment IDs from one page into the local downloads folder.

        Pass page as a numeric ID or same-instance URL, and attachment_ids from a typed reference or
        the page's attachment list. The tool verifies ownership on that page. Up to 500 IDs may be
        selected; successful files return local paths and failures are listed per attachment.
        """
        services = await context.services()
        result: AttachmentDownloadResult = await services.attachments.download_attachments(
            page, attachment_ids
        )
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def upload_attachments(
        page: Annotated[str, Field(description="Current page ID or supported same-instance page URL.")],
        file_paths: Annotated[
            list[str],
            Field(min_length=1, max_length=100, description="One to 100 absolute local file paths with unique filenames."),
        ],
        comment: Annotated[str | None, Field(max_length=255, description="Optional attachment version comment.")] = None,
        notify_watchers: Annotated[bool, Field(description="Notify watchers of attachment versions; false sends minorEdit=true.")] = True,
    ) -> AttachmentMutationResult:
        """Upload new attachments or add versions when a filename already exists on the page.

        Files are read from the supplied local paths. The instance enforces its own size limit. Results
        include each attachment ID, version, media type and download URL, plus per-file failures. Set
        notify_watchers=false to send minorEdit=true for the version; the result echoes the request,
        which Confluence does not read back. To
        show an image inline (PNG, JPEG, GIF or SVG), upload it and reference it as
        `<ac:image><ri:attachment ri:filename="name.svg"/></ac:image>`. Do not use the `view-file`
        macro or raw `<svg>` markup: Confluence displays the former as a file icon and strips the latter.
        """
        services = await context.services()
        return await services.attachment_management.upload_attachments(
            page, file_paths, comment=comment, notify_watchers=notify_watchers
        )

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def update_attachment(
        page: Annotated[str, Field(description="Current page ID or supported same-instance page URL.")],
        attachment_id: Annotated[str, Field(description="Attachment ID belonging to this page.")],
        filename: Annotated[str | None, Field(max_length=255, description="New filename; omit to keep the current name.")] = None,
        comment: Annotated[str | None, Field(max_length=255, description="New version comment; omit to keep the current comment.")] = None,
        media_type: Annotated[str | None, Field(max_length=255, description="New MIME type, such as image/svg+xml.")] = None,
        notify_watchers: Annotated[bool, Field(description="Notify watchers of the metadata version; false sends minorEdit=true.")] = True,
    ) -> AttachmentMutationResult:
        """Rename or update metadata without uploading bytes; false suppresses version email where supported."""
        services = await context.services()
        return await services.attachment_management.update_attachment(
            page, attachment_id, filename=filename, comment=comment, media_type=media_type,
            notify_watchers=notify_watchers,
        )

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def trash_attachments(
        page: Annotated[str, Field(description="Current page ID or supported same-instance page URL.")],
        attachment_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        allow_referenced: Annotated[
            bool,
            Field(description="Confirm trashing attachments still referenced in this page's body (default false)."),
        ] = False,
        notify_watchers: Annotated[bool, Field(description="This trash route has no notification-suppression control.")] = True,
    ) -> AttachmentMutationResult:
        """Move selected page attachments to the space trash; this can be restored later.

        IDs must belong to the named page. By default attachments referenced by that page's current
        body are refused. The tool checks the separate attachment delete permission before each write.
        References from other pages are not searched and are called out in the result. This route has
        no notification-suppression control; its result reports that limitation.
        """
        services = await context.services()
        return await services.attachment_management.trash_attachments(
            page, attachment_ids, allow_referenced=allow_referenced, notify_watchers=notify_watchers
        )

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def restore_attachments(
        page: Annotated[str, Field(description="Original page ID or supported same-instance page URL.")],
        attachment_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        notify_watchers: Annotated[bool, Field(description="Notify watchers of the restore version; false sends minorEdit=true.")] = True,
    ) -> AttachmentMutationResult:
        """Restore selected attachments; false sends minorEdit=true on each restore version."""
        services = await context.services()
        return await services.attachment_management.restore_attachments(
            page, attachment_ids, notify_watchers=notify_watchers
        )

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def export_pages(
        pages: Annotated[
            list[str] | str,
            Field(description="One to 500 numeric page IDs or supported same-instance page URLs."),
        ],
        format: Annotated[
            PageTextFormat | None,
            Field(description="text (default) or rich storage markup."),
        ] = None,
        parse_scope: Annotated[
            PageParseScope | None,
            Field(description="Choose exported sections such as header, attachments and comments."),
        ] = None,
        include_attachments: Annotated[
            bool,
            Field(description="Download attachment bytes into the export (default false)."),
        ] = False,
    ) -> PageExportResult:
        """Export up to 500 pages in full as local files or a ZIP archive.

        Text is the default. Rich writes an editable body file with its provenance header and writes
        selected comments to a separate read-only file. Rich exports of up to 20 pages without
        attachment bytes are separate local files; bulk and attachment exports are ZIP archives.
        The result lists every output file with its role and local path. Attachment bytes are excluded
        unless include_attachments is true. Page and attachment failures appear in a report file.
        Large exports may take minutes.
        """
        services = await context.services()
        result: PageExportResult = await services.exports.export_pages(
            pages,
            format=format,
            parse_scope=parse_scope,
            include_attachments=include_attachments,
        )
        return result

    @mcp.tool(annotations=WRITE)
    @_logged
    async def create_page(
        ctx: Context,
        title: Annotated[str, Field(max_length=255, description="New page title, up to 255 characters.")],
        body: Annotated[
            str | None,
            Field(
                max_length=2_000_000,
                description="Complete page body, up to 2 million characters; use exactly one of body and body_file.",
            ),
        ] = None,
        format: Annotated[
            PageTextFormat | None,
            Field(description="rich HTML/storage markup (default) or escaped plain text."),
        ] = None,
        status: Annotated[
            Literal["current", "draft"] | None,
            Field(description="Create a published page (default) or a new-page draft."),
        ] = None,
        parent_id: Annotated[
            str | None,
            Field(description="Parent page ID/reference; use exactly one of parent_id and space_key."),
        ] = None,
        space_key: Annotated[
            str | None,
            Field(description="Target space root; use exactly one of space_key and parent_id."),
        ] = None,
        position: Annotated[
            Literal["first", "last", "before", "after"] | None,
            Field(description="Sibling position (default last); before/after require sibling."),
        ] = None,
        sibling: Annotated[
            str | None,
            Field(description="Sibling page ID/reference required for before or after position."),
        ] = None,
        body_file: Annotated[
            str | None,
            Field(description="Absolute path of a UTF-8 storage file to use as the body instead of body."),
        ] = None,
        notify_watchers: Annotated[bool, Field(description="Page creation has no notification-suppression control; reported in the result.")] = True,
    ) -> PageWriteResult:
        """Create a page or new-page draft in exactly one parent or space root.

        Rich is the default format: provide HTML and Confluence storage tags, not Markdown or wiki
        markup. The connector converts pre/code to the code macro, details/summary to expand, and
        HTML void tags to self-closing tags. It refuses scripts, styles, frames, objects, embeds,
        event handlers, javascript URLs and the Confluence HTML macro. Storage can use code, expand,
        panels, status, table of contents, task lists, page/attachment links, images and mentions.
        Text is escaped and split into paragraphs. A duplicate title in the target space is refused.
        Position defaults to last; before/after require sibling in the same parent.
        body_file sends a rich storage file unchanged apart from new pre/details blocks; a rich
        export's header is dropped and its storage kept as is. Page creation has no watcher-suppression
        control; the result echoes notify_watchers and reports this limitation.
        """
        services = await context.services()
        result: PageWriteResult = await services.writes.create_page(
            title,
            body,
            format=format if format is not None else "rich",
            status=status if status is not None else "current",
            parent_id=parent_id,
            space_key=space_key,
            position=position,
            sibling=sibling,
            body_file=body_file,
            body_file_roots=await _client_roots(ctx) if body_file is not None else (),
            notify_watchers=notify_watchers,
        )
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def update_page(
        ctx: Context,
        page: Annotated[
            str,
            Field(description="Published page ID or same-instance URL; a body_file must come from this page."),
        ],
        body: Annotated[
            str | None,
            Field(
                max_length=2_000_000,
                description="Replacement body, up to 2 million characters; use exactly one of body and body_file.",
            ),
        ] = None,
        expected_version: Annotated[
            int | None,
            Field(ge=1, description="Current page version, required with body; body_file supplies it."),
        ] = None,
        format: Annotated[
            PageTextFormat | None,
            Field(description="rich HTML/storage markup (default) or escaped plain text."),
        ] = None,
        title: Annotated[str | None, Field(max_length=255, description="Optional replacement title.")] = None,
        allow_comment_loss: Annotated[
            bool,
            Field(description="Confirm if replacement would detach inline-comment anchors."),
        ] = False,
        body_file: Annotated[
            str | None,
            Field(description="Absolute path of an edited rich export_pages file, instead of body."),
        ] = None,
        notify_watchers: Annotated[bool, Field(description="Notify watchers of the edit; false sends version.minorEdit=true.")] = True,
    ) -> PageWriteResult:
        """Replace a published page's complete body, with an optional title change.

        body_file takes the body file from a rich export_pages result of the same page: its header's page
        must match page and its version is the expected version, and the storage is sent unchanged
        apart from new pre/details blocks. Files without the header, text exports and files with the
        comments section are refused; separate comments files have no header and are refused. With body,
        expected_version is required. A stale version is
        never overwritten. Rich is the default:
        provide HTML and Confluence storage tags, not Markdown or wiki markup. The connector converts
        pre/code to the code macro, details/summary to expand, and HTML void tags to self-closing
        tags. It refuses scripts, styles, frames, objects, embeds, event handlers, javascript URLs
        and the Confluence HTML macro. Storage can use code, expand, panels, status, table of
        contents, task lists, page/attachment links, images and mentions. Text is escaped and split
        into paragraphs. Inline-comment anchors that would be detached require allow_comment_loss.
        Removed macros and attachment references are reported in warnings. Set notify_watchers=false
        to send version.minorEdit=true and suppress edit email; the result echoes the flag, which
        Confluence does not read back.
        """
        services = await context.services()
        result: PageWriteResult = await services.writes.update_page(
            page,
            body,
            expected_version=expected_version,
            format=format if format is not None else "rich",
            title=title,
            allow_comment_loss=allow_comment_loss,
            body_file=body_file,
            body_file_roots=await _client_roots(ctx) if body_file is not None else (),
            notify_watchers=notify_watchers,
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def list_drafts(
        space_key: Annotated[str | None, Field(description="Limit drafts to this space; optional.")] = None,
        limit: Annotated[
            int | None,
            Field(ge=1, le=100, description="Results per page, 1 through 100 (default 50)."),
        ] = None,
        cursor: Annotated[
            str | None, Field(description="Draft-list continuation cursor; pass it alone.")
        ] = None,
    ) -> DraftListResult:
        """List the signed-in user's new-page drafts; pass only cursor to continue."""
        services = await context.services()
        result: DraftListResult = await services.writes.list_drafts(
            space_key=space_key,
            limit=limit,
            cursor=cursor,
        )
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def publish_draft(
        draft: Annotated[str, Field(description="New-page draft ID or supported same-instance page URL.")],
        notify_watchers: Annotated[bool, Field(description="Notify watchers on publish; false sends version.minorEdit=true.")] = True,
    ) -> PageWriteResult:
        """Publish a draft without changing its page ID; false suppresses the publish email."""
        services = await context.services()
        result: PageWriteResult = await services.writes.publish_draft(draft, notify_watchers=notify_watchers)
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def delete_draft(
        draft: Annotated[str, Field(description="New-page draft ID or supported same-instance page URL.")],
        notify_watchers: Annotated[bool, Field(description="This delete route has no notification-suppression control.")] = True,
    ) -> DraftDeletionResult:
        """Permanently delete a draft. This bypasses the trash; notification suppression is unavailable."""
        services = await context.services()
        result: DraftDeletionResult = await services.writes.delete_draft(draft, notify_watchers=notify_watchers)
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def set_labels(
        page: Annotated[str, Field(description="Published page ID or supported same-instance page URL.")],
        add: Annotated[
            list[str] | None,
            Field(description="Global labels to add, one operation per label."),
        ] = None,
        remove: Annotated[
            list[str] | None,
            Field(description="Global labels to remove; removals are destructive."),
        ] = None,
        notify_watchers: Annotated[bool, Field(description="This label route has no notification-suppression control.")] = True,
    ) -> LabelChangeResult:
        """Add or remove global labels on one published page; removals are destructive.

        Changes run sequentially and stop at the first failure. Prior successful operations remain
        applied and are reported; the connector does not reverse them because Confluence does not
        identify whether this request caused a label transition. Inspect partial results before retrying.
        This route has no notification-suppression control; the result reports that limitation.
        """
        services = await context.services()
        result: LabelChangeResult = await services.writes.set_labels(
            page,
            add=add,
            remove=remove,
            notify_watchers=notify_watchers,
        )
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def move_page(
        page: Annotated[str, Field(description="Current page ID or supported same-instance page URL.")],
        parent_id: Annotated[
            str | None,
            Field(description="Target parent page; use exactly one of parent_id and space_key."),
        ] = None,
        space_key: Annotated[
            str | None,
            Field(description="Target space root; use exactly one of space_key and parent_id."),
        ] = None,
        position: Annotated[
            Literal["first", "last", "before", "after"] | None,
            Field(description="Sibling position (default last); before/after require sibling."),
        ] = None,
        sibling: Annotated[
            str | None,
            Field(description="Sibling page ID/reference required for before or after position."),
        ] = None,
        notify_watchers: Annotated[bool, Field(description="The move action has no notification-suppression control.")] = True,
    ) -> PageMoveResult:
        """Move one current page under a parent or to a space root.

        Pass exactly one of parent_id or space_key. Position defaults to last; before/after require
        a sibling in the selected parent. Same-space moves use one action without creating a version;
        a definite refusal preserves placement. Cross-space moves are waited on through Confluence's
        long-task status route. If Confluence refuses one, recreating the page in the target space
        and trashing the original is a separate recovery plan; it loses the page ID, history and
        comments. The move action has no notification-suppression control; the result reports that limitation.
        """
        services = await context.services()
        result: PageMoveResult = await services.structure.move_page(
            page,
            parent_id=parent_id,
            space_key=space_key,
            position=position,
            sibling=sibling,
            notify_watchers=notify_watchers,
        )
        return result

    @mcp.tool(annotations=DESTRUCTIVE_WRITE)
    @_logged
    async def delete_page(
        page: Annotated[str, Field(description="Current page ID or supported same-instance page URL.")],
        children: Annotated[
            Literal["refuse", "move_up", "trash"] | None,
            Field(description="Direct-child handling: refuse (default), move_up, or trash subtree."),
        ] = None,
        notify_watchers: Annotated[bool, Field(description="The delete route has no notification-suppression control.")] = True,
    ) -> PageDeleteResult:
        """Move a page to the trash after checking delete permission on every affected page.

        children defaults to refuse. move_up reparents direct children in order before trashing the
        page. trash removes the subtree bottom-up and refuses more than 100 pages. The result reports
        each confirmed, uncertain and unattempted step; no completed step is undone automatically.
        Delete/trash routes have no notification-suppression control; the result reports that limitation.
        """
        services = await context.services()
        result: PageDeleteResult = await services.structure.delete_page(
            page,
            children=children if children is not None else "refuse",
            notify_watchers=notify_watchers,
        )
        return result

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def list_trash(
        space_key: Annotated[str | None, Field(description="Limit results to this space; optional.")] = None,
        limit: Annotated[
            int | None,
            Field(ge=1, le=100, description="Results per page, 1 through 100 (default 50)."),
        ] = None,
        cursor: Annotated[
            str | None, Field(description="Trash-list continuation cursor; pass it alone.")
        ] = None,
    ) -> TrashListResult:
        """List trashed pages in one space or across spaces visible to the signed-in account.

        Results are bounded to 100 pages. Pass only the returned cursor to continue; the result has
        page IDs, titles, space and last-edit time, but Confluence exposes no deletion timestamp.
        """
        services = await context.services()
        result: TrashListResult = await services.structure.list_trash(
            space_key=space_key,
            limit=limit,
            cursor=cursor,
        )
        return result

    @mcp.tool(annotations=WRITE)
    @_logged
    async def restore_page(
        page: Annotated[str, Field(description="Trashed page ID or supported same-instance page URL.")],
        parent_id: Annotated[
            str | None,
            Field(description="Requested parent; use exactly one of parent_id and space_key."),
        ] = None,
        space_key: Annotated[
            str | None,
            Field(description="Requested space root; use exactly one of space_key and parent_id."),
        ] = None,
        position: Annotated[
            Literal["first", "last", "before", "after"] | None,
            Field(description="Sibling position (default last); before/after require sibling."),
        ] = None,
        sibling: Annotated[
            str | None,
            Field(description="Sibling page ID/reference required for before or after position."),
        ] = None,
        notify_watchers: Annotated[bool, Field(description="Notify watchers of the restore; false sends version.minorEdit=true. A later placement move cannot be silenced.")] = True,
    ) -> PageRestoreResult:
        """Restore a trashed page, then use caller placement, saved placement or the space root.

        Pass exactly one of parent_id or space_key to choose placement explicitly. Without placement,
        the connector uses the best-effort record saved during delete; Confluence itself restores at
        the space root. Set notify_watchers=false to suppress the restore version email; a later placement
        move cannot be silenced. The result states the confirmed placement and any limitation.
        """
        services = await context.services()
        result: PageRestoreResult = await services.structure.restore_page(
            page,
            parent_id=parent_id,
            space_key=space_key,
            position=position,
            sibling=sibling,
            notify_watchers=notify_watchers,
        )
        return result

    return mcp


def serve_mcp(*, context: AppContext | None = None) -> None:
    """Run the stdio server; stdout is reserved for protocol traffic."""
    build_server(context or AppContext()).run("stdio")


async def _client_roots(ctx: Context) -> list[Path]:
    """Return the local folders the client shares as MCP roots; none when it does not support roots."""
    session = ctx.session
    if not session.check_client_capability(ClientCapabilities(roots=RootsCapability())):
        return []
    try:
        result = await session.list_roots()
    except McpError as exc:
        log.info("MCP roots could not be listed (%s)", type(exc).__name__)
        return []
    return root_paths(str(root.uri) for root in result.roots)


def root_paths(uris: Iterable[str]) -> list[Path]:
    """Map `file:` root URIs to local paths, ignoring any other scheme."""
    paths: list[Path] = []
    for uri in uris:
        parts = urlsplit(uri)
        if parts.scheme.casefold() != "file":
            continue
        location = f"//{parts.netloc}{parts.path}" if parts.netloc not in ("", "localhost") else parts.path
        paths.append(Path(url2pathname(location)))
    return paths


def _logged[**P, R](function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Log tool name, duration and outcome without arguments or returned content."""

    @functools.wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.monotonic()
        try:
            result = await function(*args, **kwargs)
        except Exception as exc:
            log.warning(
                "tool %s failed (%s) in %dms",
                function.__name__,
                type(exc).__name__,
                (time.monotonic() - started) * 1000,
            )
            raise
        log.info("tool %s completed in %dms", function.__name__, (time.monotonic() - started) * 1000)
        return result

    return wrapped
