"""MCP tools for read-only Confluence search and browsing."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from confluence_connector.client import ConfluenceClient

mcp = FastMCP("Confluence PoC")


@mcp.tool()
def confluence_search_pages(query: str, limit: int = 10) -> dict[str, object]:
    """Search accessible Confluence pages by text and return titles, spaces, dates, and links."""
    with ConfluenceClient.from_environment() as client:
        return client.search_pages(query, limit)


@mcp.tool()
def confluence_list_spaces(limit: int = 50, start: int = 0) -> dict[str, object]:
    """List one page of Confluence spaces visible to the authenticated user."""
    with ConfluenceClient.from_environment() as client:
        return client.list_spaces(limit, start)


@mcp.tool()
def confluence_get_page(page_id: str) -> dict[str, object]:
    """Fetch a Confluence page by content ID, including its storage-format body."""
    with ConfluenceClient.from_environment() as client:
        return client.get_page(page_id)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()