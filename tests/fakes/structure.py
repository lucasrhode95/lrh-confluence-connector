"""Shared synthetic page hierarchy and placement adapter."""

from confluence_connector.domain.errors import NotFound, Upstream
from confluence_connector.domain.models import (ContentSearchHit, PageMetadata, PageSummary, RemoteContentSearchPage, RemoteTrashPage, ResolvedPageReference)


def _page(
    page_id: str,
    title: str,
    *,
    space_key: str = "SYNTH",
    parent_id: str | None = None,
    status: str = "current",
) -> PageMetadata:
    ancestors = [PageSummary(page_id=parent_id, title=f"Synthetic {parent_id}")] if parent_id else []
    return PageMetadata(
        page_id=page_id,
        title=title,
        content_type="page",
        status=status,
        space_key=space_key,
        version=1,
        ancestors=ancestors,
    )


class _Links:
    async def resolve(self, reference: str, *, expected_username: str, status: str | None = None) -> ResolvedPageReference:
        return ResolvedPageReference(page_id=reference)


class _StructureApi:
    def __init__(self) -> None:
        self.pages: dict[str, PageMetadata] = {}
        self.children: dict[str, list[str]] = {}
        self.permission_checks: list[str] = []
        self.delete_denied: set[str] = set()
        self.trash_calls: list[str] = []
        self.fail_trash_for: dict[str, Exception] = {}
        self.move_calls: list[dict[str, object]] = []
        self.move_error: Exception | None = None
        self.fail_move_for: set[str] = set()
        self.properties: dict[str, dict[str, object]] = {}
        self.property_writes: list[str] = []
        self.properties_removed: list[str] = []
        self.restore_calls: list[str] = []
        self.restore_arguments: list[dict[str, object]] = []
        self.trash_requests: list[tuple[str | None, int, int]] = []
        self.trash_page_response = RemoteTrashPage(pages=[], start=0, size=0, total_size=0, has_more=False)
        self.metadata_calls: list[str] = []

    def add_child(self, parent_id: str, page_id: str, title: str) -> None:
        self.pages[page_id] = _page(page_id, title, parent_id=parent_id)
        self.children.setdefault(parent_id, []).append(page_id)

    async def get_page_metadata(
        self, page_id: str, *, expected_username: str, status: str | None = None
    ) -> PageMetadata:
        self.metadata_calls.append(page_id)
        page = self.pages.get(page_id)
        if page is None or page.status != (status or "current"):
            raise NotFound("Synthetic missing page")
        return page

    async def list_child_pages(self, page_id: str, *, start: int, expected_username: str):
        return self._child_page(self.children.get(page_id, []), start)

    async def list_space_root_pages(self, space_key: str, *, start: int, expected_username: str):
        ids = [page_id for page_id, page in self.pages.items() if page.space_key == space_key and not page.ancestors and page.status == "current"]
        return self._child_page(ids, start)

    def _child_page(self, ids: list[str], start: int) -> RemoteContentSearchPage:
        rows = ids[start : start + 500]
        return RemoteContentSearchPage(
            hits=[
                ContentSearchHit(
                    content_id=page_id,
                    content_type="page",
                    title=self.pages[page_id].title,
                    space_key=self.pages[page_id].space_key,
                    parent_id=(self.pages[page_id].ancestors[-1].page_id if self.pages[page_id].ancestors else None),
                    position=start + index,
                )
                for index, page_id in enumerate(rows)
            ],
            start=start,
            size=len(rows),
            total_size=len(ids),
        )

    async def page_supports_delete(self, page_id: str, *, expected_username: str) -> bool:
        self.permission_checks.append(page_id)
        return page_id not in self.delete_denied

    async def space_permissions_readable(self, space_key: str, *, expected_username: str) -> bool:
        return True

    async def set_page_property(self, page_id: str, key: str, value: dict[str, object], *, expected_username: str) -> None:
        self.property_writes.append(page_id)
        self.properties[page_id] = value

    async def get_page_property(self, page_id: str, key: str, *, expected_username: str) -> dict[str, object]:
        if page_id not in self.properties:
            raise NotFound("Synthetic missing property")
        return self.properties[page_id]

    async def remove_page_property(self, page_id: str, key: str, *, expected_username: str) -> None:
        self.properties_removed.append(page_id)
        self.properties.pop(page_id, None)

    async def move_page_action(self, page_id: str, **kwargs) -> None:
        self.move_calls.append({"page_id": page_id, **kwargs})
        if page_id in self.fail_move_for:
            raise Upstream("Synthetic move-action failure")
        if self.move_error is not None and kwargs["position"] != "topLevel":
            raise self.move_error
        page = self.pages[page_id]
        position = kwargs["position"]
        target_id = kwargs["target_id"]
        target_parent_id = None
        if position == "append":
            target_parent_id = target_id
        elif position in {"above", "below"} and target_id in self.pages:
            target = self.pages[target_id]
            target_parent_id = target.ancestors[-1].page_id if target.ancestors else None
        page.space_key = kwargs["space_key"]
        page.ancestors = (
            [PageSummary(page_id=target_parent_id, title=f"Synthetic {target_parent_id}")]
            if target_parent_id
            else []
        )
        for children in self.children.values():
            while page_id in children:
                children.remove(page_id)
        if position == "append" and target_id is not None:
            self.children.setdefault(target_id, []).append(page_id)
        elif position in {"above", "below"} and target_id in self.children.get(target_parent_id, []):
            siblings = self.children[target_parent_id]
            siblings.insert(siblings.index(target_id) + (position == "below"), page_id)

    async def trash_page(self, page_id: str, *, expected_username: str) -> None:
        self.trash_calls.append(page_id)
        if error := self.fail_trash_for.get(page_id):
            raise error
        self.pages[page_id].status = "trashed"

    async def restore_trashed_page(self, page_id: str, **kwargs) -> PageMetadata:
        self.restore_calls.append(page_id)
        self.restore_arguments.append(kwargs)
        self.pages[page_id].status = "current"
        self.pages[page_id].ancestors = []
        return self.pages[page_id]

    async def list_trash_page(self, *, space_key: str | None, start: int, limit: int, expected_username: str):
        self.trash_requests.append((space_key, start, limit))
        return self.trash_page_response.model_copy(
            update={"start": start, "has_more": start == 0 and self.trash_page_response.has_more}
        )

