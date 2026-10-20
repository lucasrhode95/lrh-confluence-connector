"""MCP tools: thin, read-only wrappers over Confluence services."""

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
from confluence_connector.domain.models import AuthStatus, SpaceList

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
OFFLINE_READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

INSTRUCTIONS = """\
Confluence Data Center access for the signed-in local account. Confluence owns its pages, spaces,
drafts, comments and attachment references; links to other products remain links.

- auth_status reports the configured instance and the account name stored with the encrypted local token.
  It is offline; status is not proof that Confluence will accept the token.
- list_spaces returns current and archived spaces visible to the stored account. The result is paged
  internally using the server's actual page size.
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
