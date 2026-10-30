"""Offline checks for page move, trash, delete and restore coordination."""

from __future__ import annotations

import unittest

from confluence_connector.domain.errors import (
    InvalidRequest,
    PermissionDenied,
    UncertainWrite,
    Upstream,
)
from confluence_connector.domain.models import (
    RemoteTrashPage,
    TrashPageSummary,
)
from fakes.structure import _Links, _StructureApi, _page
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

    async def test_same_space_move_uses_one_action_without_creating_a_version(self) -> None:
        self.api.pages["10"] = _page("10", "Moving", parent_id="20")
        self.api.pages["30"] = _page("30", "Target")
        self.api.pages["21"] = _page("21", "Sibling", parent_id="30")
        self.api.children["30"] = ["21"]
        self.api.move_error = Upstream("Synthetic position refusal")

        result = await self.service.move_page(
            "10", parent_id="30", position="before", sibling="21", notify_watchers=False
        )

        self.assertFalse(result.moved)
        self.assertFalse(result.position_applied)
        self.assertEqual(result.parent_id, "20")
        self.assertEqual(len(self.api.move_calls), 1)
        self.assertEqual(self.api.move_calls[0]["position"], "above")
        self.assertEqual(self.api.move_calls[0]["target_id"], "21")
        self.assertEqual(self.api.pages["10"].ancestors[-1].page_id, "20")
        self.assertEqual(self.api.pages["10"].version, 1)
        self.assertFalse(result.notify_watchers)
        self.assertFalse(result.notification_suppression_supported)
        self.assertIsNotNone(result.notification_note)

    async def test_same_space_move_uncertain_result_does_not_guess_the_parent(self) -> None:
        self.api.pages["10"] = _page("10", "Moving", parent_id="20")
        self.api.pages["30"] = _page("30", "Target")
        self.api.move_error = UncertainWrite("Synthetic lost response")

        result = await self.service.move_page("10", parent_id="30")

        self.assertFalse(result.moved)
        self.assertIsNone(result.position_applied)
        self.assertIsNone(result.parent_id)
        self.assertEqual(len(self.api.move_calls), 1)
        self.assertEqual(self.api.pages["10"].version, 1)

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
        self.assertEqual(self.api.move_calls[0]["position"], "below")
        self.assertEqual(self.api.move_calls[0]["target_id"], "20")
        self.assertEqual(self.api.pages["10"].version, 1)

    async def test_first_position_uses_the_first_sibling_after_the_moving_page(self) -> None:
        self.api.pages["10"] = _page("10", "Moving")
        self.api.pages["11"] = _page("11", "Next root page")

        result = await self.service.move_page("10", space_key="SYNTH", position="first")

        self.assertTrue(result.moved)
        self.assertEqual(self.api.move_calls[0]["position"], "above")
        self.assertEqual(self.api.move_calls[0]["target_id"], "11")

    async def test_move_placement_rejects_a_descendant_parent_before_writing(self) -> None:
        self.api.pages["10"] = _page("10", "Moving")
        self.api.pages["11"] = _page("11", "Descendant", parent_id="10")

        with self.assertRaisesRegex(InvalidRequest, "itself or one of its descendants"):
            await self.service.move_page("10", parent_id="11")

        self.assertEqual(self.api.move_calls, [])

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
        self.assertEqual(
            [(call["page_id"], call["position"], call["target_id"]) for call in self.api.move_calls],
            [("11", "append", "20"), ("12", "append", "20")],
        )
        self.assertEqual([self.api.pages[page_id].version for page_id in ("11", "12")], [1, 1])
        self.assertEqual(self.api.trash_calls, ["10"])
        self.assertEqual([item.status for item in result.pages], ["trashed", "moved_up", "moved_up"])
        self.assertEqual([item.former_parent_id for item in result.pages], ["20", "10", "10"])
        self.assertEqual([item.former_position for item in result.pages], [0, 0, 1])

    async def test_delete_stops_after_exact_partial_child_move_failure(self) -> None:
        self.api.pages["10"] = _page("10", "Parent", parent_id="20")
        self.api.add_child("10", "11", "First")
        self.api.add_child("10", "12", "Second")
        self.api.fail_move_for.add("12")

        result = await self.service.delete_page("10", children="move_up")

        self.assertFalse(result.complete)
        self.assertEqual([item.status for item in result.pages], ["not_attempted", "moved_up", "failed"])
        self.assertEqual(self.api.trash_calls, [])
        self.assertIn("Synthetic move-action failure", result.failure or "")

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

        result = await self.service.delete_page("10", children="trash", notify_watchers=False)

        self.assertTrue(result.complete)
        self.assertEqual(self.api.trash_calls, ["12", "11", "10"])
        self.assertEqual(len(self.api.properties), 3)
        self.assertEqual(self.api.properties["11"]["parent_id"], "10")
        self.assertEqual(self.api.properties["10"]["parent_id"], "20")
        self.assertFalse(result.notify_watchers)
        self.assertFalse(result.notification_suppression_supported)
        self.assertIsNotNone(result.notification_note)

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
        self.assertEqual(len(self.api.move_calls), 1)
        self.assertEqual(self.api.pages["90"].version, 1)
        self.assertEqual(self.api.move_calls[0]["position"], "above")
        self.assertEqual(self.api.move_calls[0]["target_id"], "21")

    async def test_restore_uses_saved_placement_and_falls_back_when_parent_is_gone(self) -> None:
        self.api.pages["90"] = _page("90", "Restored", status="trashed")
        self.api.properties["90"] = {
            "space_key": "SYNTH",
            "parent_id": "404",
            "position": "last",
            "sibling_id": None,
        }

        result = await self.service.restore_page("90", notify_watchers=False)

        self.assertEqual(result.placement_source, "space_root")
        self.assertEqual(result.placement_status, "root")
        self.assertTrue(any("remains at the space root" in warning for warning in result.warnings))
        self.assertEqual(self.api.move_calls, [])
        self.assertEqual(self.api.properties_removed, ["90"])
        self.assertFalse(result.notify_watchers)
        self.assertTrue(result.notification_suppression_supported)
        self.assertFalse(self.api.restore_arguments[0]["notify_watchers"])

    async def test_restore_rereads_current_metadata_when_success_response_omits_space_and_version(self) -> None:
        self.api.pages["90"] = _page("90", "Restored", status="trashed")
        self.api.pages["30"] = _page("30", "Target")
        original = self.api.restore_trashed_page
        async def sparse_restore(page_id, **kwargs):
            restored = await original(page_id, **kwargs)
            return restored.model_copy(update={"space_key": None, "version": None})
        self.api.restore_trashed_page = sparse_restore
        result = await self.service.restore_page("90", parent_id="30")
        self.assertEqual(result.placement_status, "applied")
        self.assertEqual(result.parent_id, "30")
        self.assertEqual(len(self.api.move_calls), 1)
        self.assertEqual(self.api.move_calls[0]["position"], "append")
        self.assertEqual(self.api.move_calls[0]["target_id"], "30")
        self.assertEqual(self.api.pages["90"].version, 1)

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
        self.assertEqual(self.api.move_calls[0]["target_id"], "21")
        self.assertEqual(self.api.pages["90"].version, 1)
        self.assertEqual(self.api.properties_removed, ["90"])




if __name__ == "__main__":
    unittest.main()
