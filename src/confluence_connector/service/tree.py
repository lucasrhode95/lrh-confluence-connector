"""Depth-bounded, ordered page trees for a page or a Confluence space root."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from confluence_connector.domain.errors import InvalidRequest, Upstream
from confluence_connector.domain.models import (
    ContentSearchHit,
    PageSummary,
    PageTreeNode,
    PageTreeResult,
    RemoteContentSearchPage,
)
from confluence_connector.domain.validation import page_reference, space_key as validate_space_key
from confluence_connector.remote.confluence import CHILD_PAGE_SIZE, ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver

DEFAULT_DEPTH = 3
MAX_DEPTH = 5
MAX_TREE_PAGES = 1_000
TREE_LISTING_CONCURRENCY = 6


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
            reference = page_reference(page)
            resolved = await self._links.resolve(reference, expected_username=self._username)
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
            children = await self._build(
                lambda start: self._api.list_child_pages(page_id, start=start, expected_username=self._username),
                max_depth=depth,
                state=state,
            )
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

        key = validate_space_key(space_key)
        state = _TreeState()
        children = await self._build(
            lambda start: self._api.list_space_root_pages(key, start=start, expected_username=self._username),
            max_depth=depth,
            state=state,
        )
        return PageTreeResult(
            root=None,
            space_key=key,
            children=children,
            max_depth=depth,
            total_pages=state.total_pages,
            complete=state.complete,
            notes=_tree_notes(state, depth),
        )

    async def _build(
        self,
        fetch_top: Callable[[int], Awaitable[RemoteContentSearchPage]],
        *,
        max_depth: int,
        state: _TreeState,
    ) -> list[PageTreeNode]:
        """Read the tree level by level below the root, listing up to six parents' children at once.

        Server order is kept within each parent. When the page cap is reached, shallower levels are
        complete and the deepest level read is cut short.
        """
        top: list[PageTreeNode] = []
        _admit(await self._list_children(fetch_top, state.remaining), top, state)
        level = top
        depth = 1
        while level and depth < max_depth and state.complete:
            level = await self._read_level(level, state)
            depth += 1
        return top

    async def _read_level(self, level: list[PageTreeNode], state: _TreeState) -> list[PageTreeNode]:
        """Attach children to one level's parents in order and return the next level.

        Parents are listed in windows of six. Each window reads at most one result past the budget
        left before it, and no window starts after the cap is reached, so remote work stays bounded.
        """
        next_level: list[PageTreeNode] = []
        for offset in range(0, len(level), TREE_LISTING_CONCURRENCY):
            window = level[offset : offset + TREE_LISTING_CONCURRENCY]
            budget = state.remaining
            listings = await self._gather(
                [
                    self._list_children(
                        lambda start, page_id=node.page_id: self._api.list_child_pages(
                            page_id, start=start, expected_username=self._username
                        ),
                        budget,
                    )
                    for node in window
                ]
            )
            for node, children in zip(window, listings, strict=True):
                _admit(children, node.children, state)
                next_level.extend(node.children)
                if not state.complete:
                    return next_level
        return next_level

    async def _list_children(
        self, fetch_page: Callable[[int], Awaitable[RemoteContentSearchPage]], budget: int
    ) -> list[PageTreeNode]:
        """List one parent's children in server order, stopping once more than ``budget`` are read."""
        nodes: list[PageTreeNode] = []
        start = 0
        while len(nodes) <= budget:
            result = await fetch_page(start)
            nodes.extend(_tree_node(hit) for hit in result.hits)
            next_start = result.next_start(CHILD_PAGE_SIZE)
            if next_start is None:
                break
            start = next_start
        return nodes

    async def _gather(
        self, listings: list[Coroutine[Any, Any, list[PageTreeNode]]]
    ) -> list[list[PageTreeNode]]:
        """Run one window's child listings at once; one failure cancels the rest and propagates."""
        tasks = [asyncio.create_task(listing) for listing in listings]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


class _TreeState:
    """Mutable per-call counters; never retained across MCP calls."""

    def __init__(self) -> None:
        self.total_pages = 0
        self.complete = True
        self.cap_reached_with_more = False

    @property
    def remaining(self) -> int:
        return MAX_TREE_PAGES - self.total_pages


def _admit(candidates: list[PageTreeNode], target: list[PageTreeNode], state: _TreeState) -> None:
    """Append nodes in order until the page cap; a cut marks the tree incomplete."""
    for node in candidates:
        if state.total_pages >= MAX_TREE_PAGES:
            state.complete = False
            state.cap_reached_with_more = True
            return
        state.total_pages += 1
        target.append(node)


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


def _depth(value: Any) -> int:
    if value is None:
        return DEFAULT_DEPTH
    if type(value) is not int or not 1 <= value <= MAX_DEPTH:
        raise InvalidRequest(f"max_depth must be an integer from 1 through {MAX_DEPTH}.")
    return value


def _tree_notes(state: _TreeState, depth: int) -> list[str]:
    notes = [f"The tree includes {depth} level(s) below the root."]
    if state.cap_reached_with_more:
        notes.append(f"The result reached the {MAX_TREE_PAGES}-page safety cap and is incomplete.")
    return notes
