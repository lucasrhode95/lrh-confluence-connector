"""Resolve a validated page placement to current Confluence page IDs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from confluence_connector.domain.errors import InvalidRequest, Upstream
from confluence_connector.domain.models import (
    ContentSearchHit,
    PageMetadata,
    PagePlacement,
    RemoteContentSearchPage,
    ResolvedPageReference,
)
from confluence_connector.domain.validation import require_current_page
from confluence_connector.remote.confluence import CHILD_PAGE_SIZE, ConfluenceApi

PageResolver = Callable[[str], Awaitable[ResolvedPageReference]]
PlacementPosition = Literal["first", "last", "before", "after"]


class PagePlacementResolver:
    """Resolve validated placement inputs shared by page creation, movement and restoration."""

    def __init__(self, api: ConfluenceApi, username: str) -> None:
        self._api = api
        self._username = username

    async def resolve(
        self,
        *,
        parent_reference: str | None,
        space_key: str | None,
        position: PlacementPosition,
        sibling_reference: str | None,
        resolve_page: PageResolver,
        allow_missing_status: bool = False,
        moving_page_id: str | None = None,
        resolve_root_last: bool = False,
    ) -> PagePlacement:
        """Resolve boundary-validated inputs; callers keep their page-reference policy."""
        parent_id, selected_space = await self._resolve_parent(
            parent_reference=parent_reference,
            space_key=space_key,
            resolve_page=resolve_page,
            allow_missing_status=allow_missing_status,
            moving_page_id=moving_page_id,
        )
        sibling_id = await self._resolve_position_target(
            parent_id=parent_id,
            space_key=selected_space,
            position=position,
            sibling_reference=sibling_reference,
            resolve_page=resolve_page,
            allow_missing_status=allow_missing_status,
            moving_page_id=moving_page_id,
            resolve_root_last=resolve_root_last,
        )
        return PagePlacement(
            space_key=selected_space,
            parent_id=parent_id,
            position=position,
            sibling_id=sibling_id,
        )

    async def _resolve_parent(
        self,
        *,
        parent_reference: str | None,
        space_key: str | None,
        resolve_page: PageResolver,
        allow_missing_status: bool,
        moving_page_id: str | None,
    ) -> tuple[str | None, str]:
        if parent_reference is None:
            assert space_key is not None
            return None, space_key

        parent = await resolve_page(parent_reference)
        metadata = await self._metadata(parent.page_id)
        require_current_page(metadata, allow_missing_status=allow_missing_status)
        if metadata.space_key is None:
            raise Upstream("Confluence did not return the target parent's space.")
        if moving_page_id is not None and (
            metadata.page_id == moving_page_id
            or any(ancestor.page_id == moving_page_id for ancestor in metadata.ancestors)
        ):
            raise InvalidRequest("A page cannot be moved under itself or one of its descendants.")
        return metadata.page_id, metadata.space_key

    async def _resolve_position_target(
        self,
        *,
        parent_id: str | None,
        space_key: str,
        position: PlacementPosition,
        sibling_reference: str | None,
        resolve_page: PageResolver,
        allow_missing_status: bool,
        moving_page_id: str | None,
        resolve_root_last: bool,
    ) -> str | None:
        if position in {"before", "after"}:
            assert sibling_reference is not None
            sibling = await resolve_page(sibling_reference)
            metadata = await self._metadata(sibling.page_id)
            require_current_page(metadata, allow_missing_status=allow_missing_status)
            sibling_parent_id = metadata.ancestors[-1].page_id if metadata.ancestors else None
            if metadata.space_key != space_key or sibling_parent_id != parent_id:
                raise InvalidRequest("The sibling must be in the same space and under the selected parent.")
            if moving_page_id is not None and metadata.page_id == moving_page_id:
                raise InvalidRequest("A page cannot be positioned before or after itself.")
            return metadata.page_id

        if position == "first":
            return await self._sibling_id(
                parent_id=parent_id,
                space_key=space_key,
                first=True,
                moving_page_id=moving_page_id,
            )
        if position == "last" and parent_id is None and resolve_root_last:
            return await self._sibling_id(
                parent_id=None,
                space_key=space_key,
                first=False,
                moving_page_id=moving_page_id,
            )
        return None

    async def _metadata(self, page_id: str) -> PageMetadata:
        return await self._api.get_page_metadata(page_id, expected_username=self._username)

    async def _sibling_id(
        self,
        *,
        parent_id: str | None,
        space_key: str,
        first: bool,
        moving_page_id: str | None,
    ) -> str | None:
        start = 0
        selected: str | None = None
        while True:
            page = await self._list_siblings(parent_id, space_key, start)
            for hit in page.hits:
                page_id = _page_id(hit)
                if page_id == moving_page_id:
                    continue
                if first:
                    return page_id
                selected = page_id

            next_start = page.next_start(CHILD_PAGE_SIZE)
            if next_start is None:
                return selected
            start = next_start

    async def _list_siblings(
        self, parent_id: str | None, space_key: str, start: int
    ) -> RemoteContentSearchPage:
        if parent_id is None:
            return await self._api.list_space_root_pages(
                space_key, start=start, expected_username=self._username
            )
        return await self._api.list_child_pages(
            parent_id, start=start, expected_username=self._username
        )


def _page_id(hit: ContentSearchHit) -> str:
    if hit.content_type not in (None, "page") or hit.content_id is None or not hit.content_id.isdecimal():
        raise Upstream("Confluence returned a malformed page in the sibling list.")
    return hit.content_id
