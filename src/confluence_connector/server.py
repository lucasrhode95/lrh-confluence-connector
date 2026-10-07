"""MCP tools for read-only Confluence search and browsing."""

from __future__ import annotations

from argparse import ArgumentParser

from mcp.server.fastmcp import FastMCP

from confluence_connector.client import ConfluenceClient
from confluence_connector.config import add_config_argument, load_config

mcp = FastMCP("Confluence connector PoC")


@mcp.tool()
def confluence_search_pages(query: str, limit: int = 10) -> dict[str, object]:
    """Search accessible Confluence pages by text and return titles, spaces, dates, and links."""
    with ConfluenceClient.from_configuration() as client:
        return client.search_pages(query, limit)


@mcp.tool()
def confluence_list_spaces(limit: int = 50, start: int = 0) -> dict[str, object]:
    """List one page of Confluence spaces visible to the authenticated user."""
    with ConfluenceClient.from_configuration() as client:
        return client.list_spaces(limit, start)


@mcp.tool()
def confluence_get_page(page_id: str) -> dict[str, object]:
    """Fetch a Confluence page by content ID, including its storage-format body."""
    with ConfluenceClient.from_configuration() as client:
        return client.get_page(page_id)


def main() -> None:
    """Run the MCP server over stdio."""
    parser = ArgumentParser(description=__doc__)
    add_config_argument(parser)
    args = parser.parse_args()
    load_config(args.config)
    mcp.run("stdio")


if __name__ == "__main__":
    main()