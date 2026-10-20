"""Offline checks for page, draft and label write behavior."""

from __future__ import annotations

import unittest

from confluence_connector.domain.errors import (
    AccountMismatch,
    Conflict,
    InvalidRequest,
    NotFound,
    UncertainWrite,
    Upstream,
)
from confluence_connector.domain.models import (
    ContentSearchHit,
    PageMetadata,
    PageSummary,
    RemoteContentSearchPage,
    RemoteDraftPage,
    RemotePageContent,
    ResolvedPageReference,
    DraftSummary,
)
from confluence_connector.service.cursors import encode
from confluence_connector.service.writes import PageWrites


class WriteServiceChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api = _WriteApi()
        self.service = PageWrites(self.api, _Links(self.api), "sample-user", "https://confluence.example.test/wiki")

    async def test_create_page_converts_text_and_uses_parent_space(self) -> None:
        result = await self.service.create_page("New page", "<safe>", format="text", parent_id="100")

        self.assertEqual(result.page_id, "700")
        self.assertEqual(result.status, "current")
        self.assertEqual(self.api.create_calls[0]["space_key"], "SYNTH")
        self.assertEqual(self.api.create_calls[0]["parent_id"], "100")
        self.assertEqual(self.api.create_calls[0]["storage_body"], "<p>&lt;safe&gt;</p>")
        self.assertEqual(self.api.title_checks, [("SYNTH", "New page")])

    async def test_duplicate_title_fails_before_page_creation_and_returns_link(self) -> None:
        self.api.title_matches[("SYNTH", "Existing")] = "123"

        with self.assertRaisesRegex(InvalidRequest, "spaces/SYNTH/pages/123"):
            await self.service.create_page("Existing", "body", format="text", space_key="SYNTH")

        self.assertEqual(self.api.create_calls, [])

    async def test_creation_requires_exactly_one_placement_before_requests(self) -> None:
        for kwargs in ({}, {"parent_id": "100", "space_key": "SYNTH"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidRequest):
                await self.service.create_page("New", "body", **kwargs)
        self.assertEqual(self.api.metadata_calls, [])
        self.assertEqual(self.api.title_checks, [])

    async def test_invalid_sibling_is_rejected_before_parent_is_read(self) -> None:
        for sibling in ("", "   ", "x" * 2_049, "bad\nreference"):
            with self.subTest(sibling=sibling[:20]), self.assertRaises(InvalidRequest):
                await self.service.create_page(
                    "New", "body", format="text", parent_id="100", position="before", sibling=sibling
                )
        self.assertEqual(self.api.metadata_calls, [])
        self.assertEqual(self.api.title_checks, [])
        self.assertEqual(self.api.create_calls, [])

    async def test_create_refuses_active_content_and_invalid_title_before_remote_calls(self) -> None:
        for title, body in (("Valid", "<script>unsafe</script>"), (" \n ", "body")):
            with self.subTest(title=title), self.assertRaises(InvalidRequest):
                await self.service.create_page(title, body, space_key="SYNTH")
        self.assertEqual(self.api.metadata_calls, [])
        self.assertEqual(self.api.title_checks, [])
        self.assertEqual(self.api.create_calls, [])

    async def test_create_refuses_excessive_rich_markup_nesting_before_remote_calls(self) -> None:
        depth = 129
        body = "<div>" * depth + "content" + "</div>" * depth

        with self.assertRaisesRegex(InvalidRequest, "nested more than 128 elements"):
            await self.service.create_page("New", body, space_key="SYNTH")

        self.assertEqual(self.api.metadata_calls, [])
        self.assertEqual(self.api.title_checks, [])
        self.assertEqual(self.api.create_calls, [])

    async def test_first_position_uses_a_same_space_sibling_after_creation(self) -> None:
        result = await self.service.create_page(
            "New first", "body", format="text", parent_id="100", position="first"
        )

        self.assertEqual(result.page_id, "700")
        self.assertEqual(self.api.position_calls, [("700", "SYNTH", "above", "101")])
        self.assertEqual(self.api.create_calls[0]["parent_id"], "100")

    async def test_requested_position_failure_is_reported_after_page_creation(self) -> None:
        self.api.metadata["102"] = _metadata(
            "102", title="Sibling", ancestors=[PageSummary(page_id="100", title="Parent")]
        )
        self.api.position_error = Upstream("synthetic position failure")

        result = await self.service.create_page(
            "New before", "body", format="text", parent_id="100", position="before", sibling="102"
        )

        self.assertEqual(result.page_id, "700")
        self.assertIn("position was not confirmed", result.warnings[0])
        self.assertEqual(len(self.api.create_calls), 1)

    async def test_account_change_during_positioning_preserves_created_page_result(self) -> None:
        self.api.metadata["102"] = _metadata(
            "102", title="Sibling", ancestors=[PageSummary(page_id="100", title="Parent")]
        )
        self.api.position_error = AccountMismatch("The active account changed.")

        result = await self.service.create_page(
            "New before", "body", format="text", parent_id="100", position="before", sibling="102"
        )

        self.assertEqual(result.page_id, "700")
        self.assertIn("position was not confirmed", result.warnings[0])

    async def test_before_sibling_must_share_selected_parent_and_space(self) -> None:
        self.api.metadata["102"] = _metadata(
            "102", title="Other parent child", ancestors=[PageSummary(page_id="999", title="Other")]
        )
        with self.assertRaisesRegex(InvalidRequest, "same space and under the selected parent"):
            await self.service.create_page(
                "New", "body", format="text", parent_id="100", position="before", sibling="102"
            )
        self.assertEqual(self.api.create_calls, [])

    async def test_update_refuses_stale_version_before_writing(self) -> None:
        with self.assertRaises(Conflict):
            await self.service.update_page("55", "new", expected_version=6, format="text")
        self.assertEqual(self.api.update_calls, [])

    async def test_update_refuses_lost_inline_comment_anchor_without_confirmation(self) -> None:
        self.api.pages["55"] = _page(
            "55",
            7,
            '<ac:inline-comment-marker ac:ref="comment-1">old</ac:inline-comment-marker>',
        )

        with self.assertRaisesRegex(InvalidRequest, "allow_comment_loss=true"):
            await self.service.update_page("55", "<p>replacement</p>", expected_version=7)

        self.assertEqual(self.api.update_calls, [])

    async def test_update_with_comment_loss_confirmation_reports_all_removed_references(self) -> None:
        self.api.pages["55"] = _page(
            "55",
            7,
            '<ac:inline-comment-marker ac:ref="comment-1">old</ac:inline-comment-marker>'
            '<ac:structured-macro ac:name="panel"></ac:structured-macro>'
            '<ri:attachment ri:filename="guide.pdf"/>',
        )

        result = await self.service.update_page(
            "55", "<p>replacement</p>", expected_version=7, allow_comment_loss=True
        )

        self.assertEqual(self.api.update_calls[0]["next_version"], 8)
        self.assertEqual(len(result.warnings), 3)
        self.assertIn("inline-comment anchor", result.warnings[0])
        self.assertIn("panel macro", result.warnings[1])
        self.assertIn("attachment reference", result.warnings[2])

    async def test_update_refuses_duplicate_rename_before_writing(self) -> None:
        self.api.title_matches[("SYNTH", "Taken")] = "999"

        with self.assertRaisesRegex(InvalidRequest, "spaces/SYNTH/pages/999"):
            await self.service.update_page("55", "body", expected_version=7, title="Taken", format="text")

        self.assertEqual(self.api.update_calls, [])

    async def test_update_rejects_active_content_before_page_read(self) -> None:
        with self.assertRaises(InvalidRequest):
            await self.service.update_page("55", "<script>unsafe</script>", expected_version=7)
        self.assertEqual(self.api.content_calls, [])

    async def test_draft_cursor_reuses_space_and_limit_and_advances_from_actual_size(self) -> None:
        first = await self.service.list_drafts(space_key="SYNTH", limit=2)
        second = await self.service.list_drafts(cursor=first.next_cursor)

        self.assertTrue(first.more_results_available)
        self.assertEqual([draft.page_id for draft in first.drafts], ["301", "302"])
        self.assertFalse(second.more_results_available)
        self.assertEqual(self.api.draft_calls, [("SYNTH", 0, 2), ("SYNTH", 2, 2)])

    async def test_draft_cursor_trusts_explicit_no_next_link_without_total_size(self) -> None:
        self.api.draft_page_override = RemoteDraftPage(
            drafts=[DraftSummary(page_id="301", title="Draft 301"), DraftSummary(page_id="302", title="Draft 302")],
            start=0,
            size=2,
            total_size=None,
            has_more=False,
        )

        result = await self.service.list_drafts(limit=2)

        self.assertFalse(result.more_results_available)
        self.assertFalse(result.truncated)
        self.assertIsNone(result.next_cursor)

    async def test_draft_cursor_rejects_added_scope(self) -> None:
        first = await self.service.list_drafts(limit=1)
        with self.assertRaisesRegex(InvalidRequest, "Pass only cursor"):
            await self.service.list_drafts(cursor=first.next_cursor, space_key="SYNTH")

    async def test_draft_cursor_rejects_missing_fields(self) -> None:
        cursor = encode("list_drafts", start=0, limit=10)
        with self.assertRaisesRegex(InvalidRequest, "Malformed draft cursor"):
            await self.service.list_drafts(cursor=cursor)

    async def test_draft_offset_cap_reports_truncation_without_an_invalid_cursor(self) -> None:
        self.api.draft_page_override = RemoteDraftPage(
            drafts=[DraftSummary(page_id=str(value), title=f"Draft {value}") for value in range(100)],
            start=999_950,
            size=100,
            total_size=2_000_000,
        )
        cursor = encode("list_drafts", space_key=None, start=999_950, limit=100)

        result = await self.service.list_drafts(cursor=cursor)

        self.assertTrue(result.truncated)
        self.assertFalse(result.more_results_available)
        self.assertIsNone(result.next_cursor)

    async def test_publish_draft_checks_duplicate_and_preserves_id(self) -> None:
        self.api.pages["301"] = _page("301", 1, "<p>draft</p>", status="draft")

        result = await self.service.publish_draft("301")

        self.assertEqual((result.page_id, result.status, result.version), ("301", "current", 1))
        self.assertEqual(self.api.publish_calls[0]["page_id"], "301")
        self.assertEqual(self.api.publish_calls[0]["storage_body"], "<p>draft</p>")

    async def test_publish_draft_refuses_title_collision(self) -> None:
        self.api.pages["301"] = _page("301", 1, "<p>draft</p>", status="draft")
        self.api.title_matches[("SYNTH", "Synthetic 301")] = "999"

        with self.assertRaisesRegex(InvalidRequest, "current page with the draft's title"):
            await self.service.publish_draft("301")

        self.assertEqual(self.api.publish_calls, [])

    async def test_publish_draft_refuses_a_current_page(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "new-page drafts only"):
            await self.service.publish_draft("55")
        self.assertEqual(self.api.publish_calls, [])

    async def test_delete_draft_requires_draft_status_and_reports_permanent_deletion(self) -> None:
        self.api.metadata["301"] = _metadata("301", status="draft")

        result = await self.service.delete_draft("301")

        self.assertTrue(result.deleted)
        self.assertTrue(result.permanent)
        self.assertEqual(self.api.deleted_drafts, ["301"])

    async def test_delete_draft_does_not_delete_current_page(self) -> None:
        with self.assertRaises(InvalidRequest):
            await self.service.delete_draft("55")
        self.assertEqual(self.api.deleted_drafts, [])

    async def test_set_labels_reports_completed_failed_and_unattempted_operations(self) -> None:
        self.api.pages["55"].metadata.labels = ["old", "another"]
        self.api.label_errors[("remove", "old")] = Upstream("synthetic failure")

        result = await self.service.set_labels("55", add=["new"], remove=["old", "another"])

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(result.failures[0].label, "old")
        self.assertEqual(result.not_attempted[0].label, "another")
        self.assertEqual([(item.action, item.label) for item in result.compensated], [("add", "new")])
        self.assertEqual(result.rollback_failures, [])
        self.assertEqual(self.api.label_calls[-1], ("remove", "55", "new"))

    async def test_label_compensation_runs_in_reverse_order(self) -> None:
        self.api.pages["55"].metadata.labels = ["old"]
        self.api.label_errors[("remove", "old")] = Upstream("synthetic failure")

        result = await self.service.set_labels(
            "55", add=["new", "another"], remove=["old"]
        )

        self.assertEqual(result.added, [])
        self.assertEqual([item.label for item in result.compensated], ["another", "new"])
        self.assertEqual(
            self.api.label_calls[-2:],
            [("remove", "55", "another"), ("remove", "55", "new")],
        )

    async def test_failed_label_compensation_keeps_the_applied_change_visible(self) -> None:
        self.api.pages["55"].metadata.labels = ["old"]
        self.api.label_errors[("remove", "old")] = Upstream("synthetic failure")
        self.api.label_errors[("remove", "new")] = Upstream("synthetic rollback failure")

        result = await self.service.set_labels("55", add=["new"], remove=["old"])

        self.assertEqual(result.added, ["new"])
        self.assertEqual(result.compensated, [])
        self.assertEqual(
            [(item.action, item.label) for item in result.rollback_failures],
            [("remove", "new")],
        )

    async def test_account_change_during_label_updates_preserves_partial_result(self) -> None:
        self.api.pages["55"].metadata.labels = ["old", "another"]
        self.api.label_errors[("remove", "old")] = AccountMismatch("The active account changed.")

        result = await self.service.set_labels("55", add=["new"], remove=["old", "another"])

        self.assertEqual(result.added, ["new"])
        self.assertEqual(result.failures[0].label, "old")
        self.assertEqual(result.not_attempted[0].label, "another")
        self.assertEqual(result.compensated, [])
        self.assertEqual(self.api.label_calls, [("add", "55", "new"), ("remove", "55", "old")])

    async def test_uncertain_label_failure_does_not_attempt_compensation(self) -> None:
        self.api.pages["55"].metadata.labels = ["old"]
        self.api.label_errors[("remove", "old")] = UncertainWrite("Synthetic uncertain outcome.")

        result = await self.service.set_labels("55", add=["new"], remove=["old"])

        self.assertEqual(result.added, ["new"])
        self.assertEqual(result.compensated, [])
        self.assertEqual(result.rollback_failures, [])
        self.assertEqual(self.api.label_calls, [("add", "55", "new"), ("remove", "55", "old")])

    async def test_set_labels_skips_noops_and_rejects_overlap(self) -> None:
        result = await self.service.set_labels("55", add=["old"], remove=["missing"])
        self.assertEqual(result.unchanged, ["old", "missing"])
        self.assertEqual(self.api.label_calls, [])
        with self.assertRaisesRegex(InvalidRequest, "both add and remove"):
            await self.service.set_labels("55", add=["same"], remove=["same"])

    async def test_versioned_page_reference_cannot_be_written(self) -> None:
        self.api.links_versions["55"] = 7
        with self.assertRaisesRegex(InvalidRequest, "omit pageVersion"):
            await self.service.update_page("55", "body", expected_version=7, format="text")
        self.assertEqual(self.api.content_calls, [])


def _metadata(
    page_id: str,
    *,
    title: str | None = None,
    status: str = "current",
    space_key: str = "SYNTH",
    ancestors: list[PageSummary] | None = None,
    labels: list[str] | None = None,
) -> PageMetadata:
    return PageMetadata(
        page_id=page_id,
        title=title or f"Synthetic {page_id}",
        content_type="page",
        status=status,
        space_key=space_key,
        url=f"https://confluence.example.test/wiki/spaces/{space_key}/pages/{page_id}",
        version=1,
        ancestors=ancestors or [],
        labels=labels or [],
    )


def _page(
    page_id: str,
    version: int,
    storage: str,
    *,
    status: str = "current",
) -> RemotePageContent:
    metadata = _metadata(page_id, status=status, labels=["old"])
    metadata.version = version
    return RemotePageContent(
        metadata=metadata,
        view_body=storage,
        storage_body=storage,
        version=version,
        total_versions=version,
    )


class _Links:
    def __init__(self, api: _WriteApi) -> None:
        self.api = api

    async def resolve(self, reference: str, *, expected_username: str) -> ResolvedPageReference:
        return ResolvedPageReference(
            page_id=reference,
            version=self.api.links_versions.get(reference),
        )


class _WriteApi:
    def __init__(self) -> None:
        self.pages = {"55": _page("55", 7, "<p>old</p>")}
        self.metadata = {"100": _metadata("100", title="Parent")}
        self.title_matches: dict[tuple[str, str], str] = {}
        self.links_versions: dict[str, int] = {}
        self.create_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.publish_calls: list[dict[str, object]] = []
        self.deleted_drafts: list[str] = []
        self.metadata_calls: list[tuple[str, str | None]] = []
        self.content_calls: list[str] = []
        self.title_checks: list[tuple[str, str]] = []
        self.position_calls: list[tuple[str, str, str, str]] = []
        self.position_error: Exception | None = None
        self.label_calls: list[tuple[str, str, str]] = []
        self.label_errors: dict[tuple[str, str], Exception] = {}
        self.draft_calls: list[tuple[str | None, int, int]] = []
        self.draft_rows = [
            DraftSummary(page_id="301", title="Draft 301", space_key="SYNTH"),
            DraftSummary(page_id="302", title="Draft 302", space_key="SYNTH"),
            DraftSummary(page_id="303", title="Draft 303", space_key="SYNTH"),
        ]
        self.draft_page_override: RemoteDraftPage | None = None

    async def find_page_id_by_title(self, space_key: str, title: str, *, expected_username: str) -> str | None:
        self.title_checks.append((space_key, title))
        return self.title_matches.get((space_key, title))

    async def get_page_metadata(
        self, page_id: str, *, expected_username: str, status: str | None = None
    ) -> PageMetadata:
        self.metadata_calls.append((page_id, status))
        if status == "draft":
            return self.pages.get(page_id, _page(page_id, 1, "<p>draft</p>", status="draft")).metadata
        if page_id in self.metadata:
            return self.metadata[page_id]
        if page_id in self.pages:
            return self.pages[page_id].metadata
        raise NotFound("synthetic missing")

    async def get_page_content(
        self,
        page_id: str,
        *,
        version: int | None = None,
        status: str | None = None,
        include_storage: bool = True,
        expected_username: str,
    ) -> RemotePageContent:
        self.content_calls.append(page_id)
        if status == "draft":
            return self.pages.get(page_id, _page(page_id, 1, "<p>draft</p>", status="draft"))
        return self.pages[page_id]

    async def create_page(self, **kwargs):
        self.create_calls.append(kwargs)
        metadata = _metadata("700", title=str(kwargs["title"]), status=str(kwargs["status"]), space_key=str(kwargs["space_key"]))
        metadata.version = 1
        return metadata

    async def update_page(self, page_id: str, **kwargs):
        self.update_calls.append({"page_id": page_id, **kwargs})
        return _metadata(page_id, title=str(kwargs["title"]), status="current")

    async def list_child_pages(self, page_id: str, *, start: int, expected_username: str):
        return RemoteContentSearchPage(
            hits=[ContentSearchHit(content_id="101", title="First")],
            start=start,
            size=1,
            total_size=1,
        )

    async def list_space_root_pages(self, space_key: str, *, start: int, expected_username: str):
        return RemoteContentSearchPage(hits=[], start=start, size=0, total_size=0)

    async def position_page(
        self, page_id: str, *, space_key: str, position: str, target_id: str, expected_username: str
    ) -> None:
        self.position_calls.append((page_id, space_key, position, target_id))
        if self.position_error is not None:
            raise self.position_error

    async def list_drafts_page(
        self, *, space_key: str | None, start: int, limit: int, expected_username: str
    ) -> RemoteDraftPage:
        self.draft_calls.append((space_key, start, limit))
        if self.draft_page_override is not None:
            return self.draft_page_override
        selected = self.draft_rows[start : start + limit]
        return RemoteDraftPage(drafts=selected, start=start, size=len(selected), total_size=len(self.draft_rows))

    async def publish_draft(self, page_id: str, **kwargs):
        self.publish_calls.append({"page_id": page_id, **kwargs})
        return _metadata(page_id, title=str(kwargs["title"]), status="current")

    async def delete_draft(self, page_id: str, *, expected_username: str) -> None:
        self.deleted_drafts.append(page_id)

    async def add_page_label(self, page_id: str, label: str, *, expected_username: str) -> None:
        self.label_calls.append(("add", page_id, label))
        if error := self.label_errors.get(("add", label)):
            raise error

    async def remove_page_label(self, page_id: str, label: str, *, expected_username: str) -> None:
        self.label_calls.append(("remove", page_id, label))
        if error := self.label_errors.get(("remove", label)):
            raise error


if __name__ == "__main__":
    unittest.main()
