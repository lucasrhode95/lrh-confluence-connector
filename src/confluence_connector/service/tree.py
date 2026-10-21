"""Depth-bounded, ordered page trees for a page or a Confluence space root."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from confluence_connector.domain.errors import InvalidRequest, Upstream
from confluence_connector.domain.models import (
    ContentSearchHit,
    PageSummary,
    PageTreeNode,
    PageTreeResult,
    RemoteContentSearchPage,
)
from confluence_connector.remote.confluence import CHILD_PAGE_SIZE, ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver

DEFAULT_DEPTH = 3
MAX_DEPTH = 5
MAX_TREE_PAGES = 1_000
_SPACE_KEY = re.compile(r"^(?:[A-Za-z0-9_-]{1,255}|~[A-Za-z0-9_.@-]{1,254})$")


class PageTree:
    """Resolve a root, then preserve Confluence's order while walking direct child-page routes."""

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str) -> None:
        self._api = api
        self._links = links
        self._username = username

    async def get_page_tree(
        self,
        *,
        page: str | None = None,
        space_key: str | None = None,
        max_depth: int | None = None,
    ) -> PageTreeResult:
        """Return a tree rooted at one page or at one space's top-level pages.

        Entry-point validation accepts exactly one root, bounds depth, and resolves page references
        through the configured-instance link resolver. Internal walkers receive numeric page IDs.
        """
        if (page is None) == (space_key is None):
            raise InvalidRequest("Pass exactly one of page or space_key.")
        depth = _depth(max_depth)
        if page is not None:
            if not isinstance(page, str) or not page.strip() or len(page.strip()) > 2_048:
                raise InvalidRequest("page must be a page ID or supported same-instance URL.")
            resolved = await self._links.resolve(page, expected_username=self._username)
            page_id = _page_id(resolved.page_id)
            metadata = await self._api.get_page_metadata(page_id, expected_username=self._username)
            if metadata.content_type != "page":
                raise InvalidRequest("A page tree requires a Confluence page reference.")
            root = PageSummary(
                page_id=metadata.page_id,
                title=metadata.title,
                space_key=metadata.space_key,
                url=metadata.url,
            )
            state = _TreeState()
            state.total_pages = 1
            children = await self._children(page_id, depth=1, max_depth=depth, state=state)
            notes = _tree_notes(state, depth)
            return PageTreeResult(
                root=root,
                space_key=metadata.space_key,
                ancestors=metadata.ancestors,
                children=children,
                max_depth=depth,
                total_pages=state.total_pages,
                complete=state.complete,
                notes=notes,
            )

        key = _space_key(space_key)
        state = _TreeState()
        children = await self._space_children(key, max_depth=depth, state=state)
        return PageTreeResult(
            root=None,
            space_key=key,
            children=children,
            max_depth=depth,
            total_pages=state.total_pages,
            complete=state.complete,
            notes=_tree_notes(state, depth),
        )

    async def _children(
        self, page_id: str, *, depth: int, max_depth: int, state: _TreeState
    ) -> list[PageTreeNode]:
        if depth >= max_depth or not state.complete:
            return []
        return await self._walk_children(
            lambda start: self._api.list_child_pages(
                page_id, start=start, expected_username=self._username
            ),
            depth=depth + 1,
            max_depth=max_depth,
            state=state,
        )

    async def _space_children(
        self, space_key: str, *, max_depth: int, state: _TreeState
    ) -> list[PageTreeNode]:
        if not state.complete:
            return []
        return await self._walk_children(
            lambda start: self._api.list_space_root_pages(
                space_key, start=start, expected_username=self._username
            ),
            depth=1,
            max_depth=max_depth,
            state=state,
        )

    async def _walk_children(
        self,
        fetch_page: Callable[[int], Awaitable[RemoteContentSearchPage]],
        *,
        depth: int,
        max_depth: int,
        state: _TreeState,
    ) -> list[PageTreeNode]:
        """Fetch in actual ``start + size`` order and stop once the global result cap is reached."""
        nodes: list[PageTreeNode] = []
        start = 0
        while state.complete:
            result = await fetch_page(start)
            if result.start != start or result.size != len(result.hits):
                raise Upstream("Confluence returned an inconsistent page-tree offset or size.")
            for index, hit in enumerate(result.hits):
                if state.total_pages >= MAX_TREE_PAGES:
                    state.complete = False
                    state.cap_reached_with_more = True
                    return nodes
                node = _tree_node(hit)
                state.total_pages += 1
                nodes.append(node)
                if depth < max_depth:
                    node.children = await self._children(
                        node.page_id, depth=depth, max_depth=max_depth, state=state
                    )
                if not state.complete:
                    if index + 1 < result.size:
                        state.cap_reached_with_more = True
                    return nodes
            next_start = result.start + result.size
            if result.size == 0:
                break
            more = (
                result.has_more if result.has_more is not None else (
                    next_start < result.total_size
                    if result.total_size is not None
                    else result.size == CHILD_PAGE_SIZE
                )
            )
            if not more:
                break
            if next_start <= start:
                raise Upstream("Confluence did not advance the page-tree offset.")
            if state.total_pages >= MAX_TREE_PAGES:
                state.complete = False
                state.cap_reached_with_more = True
                break
            start = next_start
        return nodes


class _TreeState:
    """Mutable per-call counters; never retained across MCP calls."""

    def __init__(self) -> None:
        self.total_pages = 0
        self.complete = True
        self.cap_reached_with_more = False


def _tree_node(hit: ContentSearchHit) -> PageTreeNode:
    if hit.content_id is None or not hit.content_id.isdecimal() or hit.content_type not in {None, "page"}:
        raise Upstream("Confluence returned a malformed child-page result.")
    return PageTreeNode(
        page_id=hit.content_id,
        title=hit.title,
        url=hit.url,
        position=hit.position,
    )


def _page_id(value: str) -> str:
    if not value.isdecimal():
        raise InvalidRequest("The selected reference did not resolve to a numeric page ID.")
    return value


def _space_key(value: Any) -> str:
    if not isinstance(value, str) or not _SPACE_KEY.fullmatch(value):
        raise InvalidRequest(
            "space_key must contain 1 to 255 letters, digits, underscores or hyphens, "
            "or a ~ personal-space key whose username may also contain dots and @."
        )
    return value


def _depth(value: Any) -> int:
    if value is None:
        return DEFAULT_DEPTH
    if type(value) is not int or not 1 <= value <= MAX_DEPTH:
        raise InvalidRequest(f"max_depth must be an integer from 1 through {MAX_DEPTH}.")
    return value


def _tree_notes(state: _TreeState, depth: int) -> list[str]:
    notes = [f"The tree includes page levels through depth {depth}."]
    if state.cap_reached_with_more:
        notes.append(f"The result reached the {MAX_TREE_PAGES}-page safety cap and is incomplete.")
    return notes
