"""Offline checks for page move, trash, delete and restore coordination."""

from __future__ import annotations

import unittest

from confluence_connector.domain.errors import (
    InvalidRequest,
    NotFound,
    PermissionDenied,
    UncertainWrite,
    Upstream,
)
from confluence_connector.domain.models import (
    ContentSearchHit,
    PageMetadata,
    PageSummary,
    RemoteContentSearchPage,
    RemoteTrashPage,
    ResolvedPageReference,
    TrashPageSummary,
)
from confluence_connector.service.structure import PageStructure


class PageStructureChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api = _StructureApi()
        self.links = _Links()
        self.service = PageStructure(self.api, self.links, "sample-user")

    async def test_move_requires_one_target_before_remote_reads(self) -> None:
        with self.assertRaises(InvalidRequest):
            await self.service.move_page("10")
        with self.assertRaises(InvalidRequest):
            await self.service.move_page("10", parent_id="20", space_key="SYNTH")
        self.assertEqual(self.api.metadata_calls, [])

    async def test_same_space_move_keeps_confirmed_parent_when_position_fails(self) -> None:
        self.api.pages["10"] = _page("10", "Moving", parent_id="20")
        self.api.pages["30"] = _page("30", "Target")
        self.api.pages["21"] = _page("21", "Sibling", parent_id="30")
        self.api.children["30"] = ["21"]
        self.api.move_error = Upstream("Synthetic position refusal")

        result = await self.service.move_page(
            "10", parent_id="30", position="before", sibling="21"
        )

        self.assertTrue(result.moved)
        self.assertFalse(result.position_applied)
        self.assertEqual(result.parent_id, "30")
        self.assertEqual(self.api.parent_updates, [("10", "30", 2)])
        self.assertEqual(self.api.move_calls[0]["position"], "above")
        self.assertEqual(self.api.move_calls[0]["target_id"], "21")

    async def test_cross_space_move_uses_async_action_and_returns_confirmed_target(self) -> None:
        self.api.pages["10"] = _page("10", "Moving")

        result = await self.service.move_page("10", space_key="OTHER", position="last")

        self.assertTrue(result.moved)
        self.assertTrue(result.position_applied)
        self.assertEqual(result.space_key, "OTHER")
        self.assertEqual(self.api.move_calls[0]["space_key"], "OTHER")
        self.assertEqual(self.api.move_calls[0]["position"], "topLevel")
        self.assertTrue(self.api.move_calls[0]["asynchronous"])

    async def test_same_space_move_to_root_uses_only_the_root_action(self) -> None:
        self.api.pages["10"] = _page("10", "Moving", parent_id="20")
        self.api.pages["20"] = _page("20", "Former parent")

        result = await self.service.move_page("10", space_key="SYNTH", position="last")

        self.assertTrue(result.moved)
        self.assertEqual(self.api.parent_updates, [])
        self.assertEqual(self.api.move_calls[0]["position"], "topLevel")
        self.assertIsNone(self.api.move_calls[0]["target_id"])

    async def test_delete_refuses_children_and_lists_them_before_permission_checks(self) -> None:
        self.api.pages["10"] = _page("10", "Parent")
        self.api.add_child("10", "11", "Child")

        with self.assertRaisesRegex(InvalidRequest, r"Child \(ID 11\)"):
            await self.service.delete_page("10")

        self.assertEqual(self.api.permission_checks, [])
        self.assertEqual(self.api.trash_calls, [])

    async def test_delete_move_up_preflights_all_pages_and_preserves_child_order(self) -> None:
        self.api.pages["10"] = _page("10", "Parent", parent_id="20")
        self.api.pages["20"] = _page("20", "Grandparent")
        self.api.children["20"] = ["10"]
        self.api.add_child("10", "11", "First")
        self.api.add_child("10", "12", "Second")

        result = await self.service.delete_page("10", children="move_up")

        self.assertTrue(result.complete)
        self.assertEqual(self.api.permission_checks, ["10", "11", "12"])
        self.assertEqual(self.api.parent_updates, [("11", "20", 2), ("12", "20", 2)])
        self.assertEqual(self.api.trash_calls, ["10"])
        self.assertEqual([item.status for item in result.pages], ["trashed", "moved_up", "moved_up"])
        self.assertEqual([item.former_parent_id for item in result.pages], ["20", "10", "10"])
        self.assertEqual([item.former_position for item in result.pages], [0, 0, 1])

    async def test_delete_stops_after_exact_partial_child_move_failure(self) -> None:
        self.api.pages["10"] = _page("10", "Parent", parent_id="20")
        self.api.add_child("10", "11", "First")
        self.api.add_child("10", "12", "Second")
        self.api.fail_parent_for.add("12")

        result = await self.service.delete_page("10", children="move_up")

        self.assertFalse(result.complete)
        self.assertEqual([item.status for item in result.pages], ["not_attempted", "moved_up", "failed"])
        self.assertEqual(self.api.trash_calls, [])
        self.assertIn("Synthetic parent-update failure", result.failure or "")

    async def test_trash_mode_checks_all_permissions_before_saving_or_deleting(self) -> None:
        self.api.pages["10"] = _page("10", "Parent")
        self.api.add_child("10", "11", "Child")
        self.api.delete_denied.add("11")

        with self.assertRaises(PermissionDenied):
            await self.service.delete_page("10", children="trash")

        self.assertEqual(self.api.permission_checks, ["10", "11"])
        self.assertEqual(self.api.property_writes, [])
        self.assertEqual(self.api.trash_calls, [])

    async def test_trash_mode_reports_exact_count_over_the_write_cap_before_permissions(self) -> None:
        self.api.pages["10"] = _page("10", "Parent")
        for page_id in range(11, 111):
            self.api.add_child("10", str(page_id), f"Child {page_id}")

        with self.assertRaisesRegex(InvalidRequest, r"101 pages, exceeding the 100-page"):
            await self.service.delete_page("10", children="trash")

        self.assertEqual(self.api.permission_checks, [])
        self.assertEqual(self.api.property_writes, [])
        self.assertEqual(self.api.trash_calls, [])

    async def test_trash_mode_deletes_bottom_up_and_retains_former_placement(self) -> None:
        self.api.pages["10"] = _page("10", "Parent", parent_id="20")
        self.api.add_child("10", "11", "First")
        self.api.add_child("11", "12", "Grandchild")

        result = await self.service.delete_page("10", children="trash")

        self.assertTrue(result.complete)
        self.assertEqual(self.api.trash_calls, ["12", "11", "10"])
        self.assertEqual(len(self.api.properties), 3)
        self.assertEqual(self.api.properties["11"]["parent_id"], "10")
        self.assertEqual(self.api.properties["10"]["parent_id"], "20")

    async def test_uncertain_trash_reports_unknown_and_stops(self) -> None:
        self.api.pages["10"] = _page("10", "Parent")
        self.api.add_child("10", "11", "Child")
        self.api.fail_trash_for["11"] = UncertainWrite("Synthetic uncertain delete")

        result = await self.service.delete_page("10", children="trash")

        self.assertFalse(result.complete)
        self.assertEqual([item.status for item in result.pages], ["not_attempted", "unknown"])
        self.assertEqual(self.api.trash_calls, ["11"])

    async def test_trash_cursor_contains_scope_and_continues_without_other_arguments(self) -> None:
        self.api.trash_page_response = RemoteTrashPage(
            pages=[TrashPageSummary(page_id="81", title="Trashed", space_key="SYNTH")],
            start=0,
            size=1,
            total_size=2,
            has_more=True,
        )

        first = await self.service.list_trash(space_key="SYNTH", limit=1)
        second = await self.service.list_trash(cursor=first.next_cursor)

        self.assertEqual((first.pages[0].page_id, second.start), ("81", 1))
        self.assertEqual(self.api.trash_requests, [("SYNTH", 0, 1), ("SYNTH", 1, 1)])
        with self.assertRaisesRegex(InvalidRequest, "Pass only cursor"):
            await self.service.list_trash(space_key="SYNTH", cursor=first.next_cursor)

    async def test_trash_listing_accepts_a_personal_space_key_prefix(self) -> None:
        self.api.trash_page_response = RemoteTrashPage(
            pages=[], start=0, size=0, total_size=0, has_more=False
        )

        result = await self.service.list_trash(space_key="~sample.user@example.test")

        self.assertEqual(result.pages, [])
        self.assertEqual(self.api.trash_requests, [("~sample.user@example.test", 0, 50)])

    async def test_restore_returns_to_root_when_caller_positioning_is_refused(self) -> None:
        self.api.pages["90"] = _page("90", "Restored", status="trashed")
        self.api.pages["30"] = _page("30", "Target")
        self.api.pages["21"] = _page("21", "Sibling", parent_id="30")
        self.api.children["30"] = ["21"]
        self.api.move_error = Upstream("Synthetic position refusal")

        result = await self.service.restore_page(
            "90", parent_id="30", position="before", sibling="21"
        )

        self.assertEqual(result.placement_source, "space_root")
        self.assertEqual(result.placement_status, "root")
        self.assertIsNone(result.parent_id)
        self.assertEqual(self.api.restore_calls, ["90"])
        self.assertEqual(self.api.properties_removed, ["90"])
        self.assertEqual(self.api.parent_updates, [("90", "30", 2)])
        self.assertEqual(self.api.move_calls[-1]["position"], "topLevel")

    async def test_restore_uses_saved_placement_and_falls_back_when_parent_is_gone(self) -> None:
        self.api.pages["90"] = _page("90", "Restored", status="trashed")
        self.api.properties["90"] = {
            "space_key": "SYNTH",
            "parent_id": "404",
            "position": "last",
            "sibling_id": None,
        }

        result = await self.service.restore_page("90")

        self.assertEqual(result.placement_source, "space_root")
        self.assertEqual(result.placement_status, "root")
        self.assertTrue(any("remains at the space root" in warning for warning in result.warnings))
        self.assertEqual(self.api.move_calls, [])
        self.assertEqual(self.api.properties_removed, ["90"])

    async def test_restore_applies_saved_placement_before_removing_the_property(self) -> None:
        self.api.pages["90"] = _page("90", "Restored", status="trashed")
        self.api.pages["30"] = _page("30", "Target")
        self.api.pages["21"] = _page("21", "Sibling", parent_id="30")
        self.api.children["30"] = ["21"]
        self.api.properties["90"] = {
            "space_key": "SYNTH",
            "parent_id": "30",
            "position": "before",
            "sibling_id": "21",
        }

        result = await self.service.restore_page("90")

        self.assertEqual(result.placement_source, "saved_property")
        self.assertEqual(result.placement_status, "applied")
        self.assertEqual(result.parent_id, "30")
        self.assertEqual(self.api.parent_updates, [("90", "30", 2)])
        self.assertEqual(self.api.move_calls[0]["target_id"], "21")
        self.assertEqual(self.api.properties_removed, ["90"])


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
        self.parent_updates: list[tuple[str, str | None, int]] = []
        self.fail_parent_for: set[str] = set()
        self.move_calls: list[dict[str, object]] = []
        self.move_error: Exception | None = None
        self.properties: dict[str, dict[str, object]] = {}
        self.property_writes: list[str] = []
        self.properties_removed: list[str] = []
        self.restore_calls: list[str] = []
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
        if page is None or status == "trashed" and page.status != "trashed":
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

    async def update_page_parent(self, page_id: str, *, title: str, next_version: int, parent_id: str | None, expected_username: str) -> PageMetadata:
        if page_id in self.fail_parent_for:
            raise Upstream("Synthetic parent-update failure")
        self.parent_updates.append((page_id, parent_id, next_version))
        page = self.pages[page_id]
        page.ancestors = [PageSummary(page_id=parent_id, title=f"Synthetic {parent_id}")] if parent_id else []
        page.version = next_version
        return page

    async def move_page_action(self, page_id: str, **kwargs) -> None:
        self.move_calls.append({"page_id": page_id, **kwargs})
        if self.move_error is not None and kwargs["position"] != "topLevel":
            raise self.move_error

    async def trash_page(self, page_id: str, *, expected_username: str) -> None:
        self.trash_calls.append(page_id)
        if error := self.fail_trash_for.get(page_id):
            raise error
        self.pages[page_id].status = "trashed"

    async def restore_trashed_page(self, page_id: str, **kwargs) -> PageMetadata:
        self.restore_calls.append(page_id)
        self.pages[page_id].status = "current"
        self.pages[page_id].ancestors = []
        return self.pages[page_id]

    async def list_trash_page(self, *, space_key: str | None, start: int, limit: int, expected_username: str):
        self.trash_requests.append((space_key, start, limit))
        return self.trash_page_response.model_copy(
            update={"start": start, "has_more": start == 0 and self.trash_page_response.has_more}
        )


if __name__ == "__main__":
    unittest.main()
