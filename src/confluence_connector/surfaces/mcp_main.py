"""MCP tools: thin wrappers over Confluence read operations and local-file outputs."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from confluence_connector import config
from confluence_connector.bootstrap import AppContext
from confluence_connector.domain.models import (
    AuthStatus,
    AttachmentDownloadResult,
    PageExportResult,
    PageParseScope,
    PageTextFormat,
    SearchFields,
    SearchMatch,
    SearchScope,
    SpaceList,
)

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
OFFLINE_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
LOCAL_FILE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)

INSTRUCTIONS = """\
Confluence Data Center access for the signed-in local account. Confluence owns its pages, spaces,
drafts, comments and attachment references; links to other products remain links.

- auth_status reports the configured instance and the account name stored with the encrypted local token.
  It is offline; status is not proof that Confluence will accept the token.
- list_spaces returns current and archived spaces visible to the stored account. The result is paged
  internally using the server's actual page size.
- search_content searches page and content fields, or titles only. It accepts literal query text,
  structured scope filters, match=all/any/phrase, and a continuation cursor. Archived spaces are
  included by default. An unscoped title search may add first-page quick-navigation results, labelled
  by source; the supplement is skipped when filters cannot be applied to that source. Date-only
  filters mean a calendar day in Confluence's timezone. ISO-8601 timestamps with offsets are accepted;
  timestamps without offsets are treated as UTC. Timestamp filters require calibrated cql_timezone;
  run `confluence-connector status --check` if it is unset.
- find_users searches current people by display name. Pass username alone for an exact lookup, including
  former users; a cursor continues a display-name search.
- get_page_tree returns ordered child pages from a page or space root, with ancestors for page roots.
  Its maximum depth is bounded; a result reports if the safety cap made it incomplete.
- get_page_metadata accepts one or more page IDs or same-instance URLs and returns page history,
  retained versions, labels, contributors and ancestors. Per-reference failures are returned alongside
  successful pages.
- get_pages reads up to 20 pages as Markdown-like text or unchanged storage markup. Text reads include
  typed attachment references and, by default, attachment and threaded comment sections; they never
  download attachment content. Use the returned cursor alone to continue an oversized page at its
  original version. Page-read timestamps remain the UTC values returned by REST.
- download_attachments saves selected attachment IDs from one page into the local downloads folder.
  Attachment IDs come from get_pages references or the page's attachment list; the tool verifies each
  ID belongs to the selected page and returns a path or per-attachment failure.
- export_pages writes full text or rich exports for up to 500 pages into the local exports folder.
  Attachment bytes are excluded by default; large exports may take minutes and incomplete items are
  listed in the ZIP report.
- Authenticate or remove the local token with `confluence-connector auth` or
  `confluence-connector auth --sign-out`. A token is never accepted through an MCP tool.
- Use only the configured Confluence instance. Page URLs on another host are refused by page tools.
"""


def build_server(context: AppContext) -> FastMCP:
    """Build the MCP surface around one process-scoped application context."""
    mcp = FastMCP(config.MCP_SERVER_NAME, instructions=INSTRUCTIONS, log_level="WARNING")

    @mcp.tool(annotations=OFFLINE_READ)
    @_logged
    async def auth_status() -> dict[str, Any]:
        """Report the local token and account status without contacting Confluence."""
        status: AuthStatus = await context.auth_status()
        return status.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def list_spaces() -> dict[str, Any]:
        """List spaces visible to the stored account; pages internally in batches of up to 500."""
        services = await context.services()
        spaces: SpaceList = await services.spaces.list_spaces()
        return spaces.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def search_content(
        query: str | None = None,
        scope: SearchScope | None = None,
        match: SearchMatch | None = None,
        fields: SearchFields | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search Confluence using literal text and structured scope filters.

        For continuation, pass only the returned cursor. `match` is all, any or phrase; `fields`
        is all or title. Scope supports spaces, space type, content types, labels, creators,
        contributors, created/modified ranges, parent/ancestor and include_archived. Date-only values
        mean a calendar day in Confluence's timezone, and mixed bounds are checked against that full
        day. ISO-8601 timestamps with offsets are accepted;
        timestamps without offsets are treated as UTC. Timestamp filters require cql_timezone; run
        `confluence-connector status --check` if it is unset. Quick-navigation supplements apply only
        to unscoped title searches.
        """
        services = await context.services()
        result = await services.search.search_content(
            query,
            scope=scope,
            match=match,
            fields=fields,
            limit=limit,
            cursor=cursor,
        )
        return result.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def find_users(
        query: str | None = None,
        username: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
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
        return result.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_page_tree(
        page: str | None = None,
        space_key: str | None = None,
        max_depth: int | None = None,
    ) -> dict[str, Any]:
        """Return ordered child pages from exactly one page reference or space root.

        Page references may be numeric IDs or supported same-instance Confluence URLs. Depth is
        bounded from 1 through 5, with a default of 3.
        """
        services = await context.services()
        result = await services.tree.get_page_tree(
            page=page,
            space_key=space_key,
            max_depth=max_depth,
        )
        return result.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_page_metadata(pages: list[str] | str) -> dict[str, Any]:
        """Return history, versions, labels, contributors and ancestors for up to 20 page references."""
        services = await context.services()
        result = await services.metadata.get_page_metadata(pages)
        return result.model_dump(mode="json")

    @mcp.tool(annotations=READ_ONLY)
    @_logged
    async def get_pages(
        pages: list[str] | str | None = None,
        format: PageTextFormat | None = None,
        max_chars: int | None = None,
        parse_scope: PageParseScope | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
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
        return result.model_dump(mode="json")

    @mcp.tool(annotations=LOCAL_FILE)
    @_logged
    async def download_attachments(page: str, attachment_ids: list[str]) -> dict[str, Any]:
        """Save selected attachment IDs from one page into the local downloads folder.

        Pass page as a numeric ID or same-instance URL, and attachment_ids from a typed reference or
        the page's attachment list. The tool verifies ownership on that page. Up to 500 IDs may be
        selected; successful files return local paths and failures are listed per attachment.
        """
        services = await context.services()
        result: AttachmentDownloadResult = await services.attachments.download_attachments(page, attachment_ids)
        return result.model_dump(mode="json")

    @mcp.tool(annotations=LOCAL_FILE)
    @_logged
    async def export_pages(
        pages: list[str] | str,
        format: PageTextFormat | None = None,
        parse_scope: PageParseScope | None = None,
        include_attachments: bool = False,
    ) -> dict[str, Any]:
        """Export up to 500 pages in full as a local file or ZIP archive.

        Text is the default; rich includes unchanged storage markup and selected comments at the end.
        The provenance header is always included. Attachment bytes are excluded unless
        include_attachments is true. Page and attachment failures appear in _export-report.txt.
        Large exports may take minutes.
        """
        services = await context.services()
        result: PageExportResult = await services.exports.export_pages(
            pages,
            format=format,
            parse_scope=parse_scope,
            include_attachments=include_attachments,
        )
        return result.model_dump(mode="json")

    return mcp


def serve_mcp(*, context: AppContext | None = None) -> None:
    """Run the stdio server; stdout is reserved for protocol traffic."""
    build_server(context or AppContext()).run("stdio")


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
