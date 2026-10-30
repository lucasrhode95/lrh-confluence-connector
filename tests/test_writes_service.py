"""Offline checks for page, draft and label write behavior."""

from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

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
from confluence_connector.service.storage_files import rich_export_content
from confluence_connector.service.writes import PageWrites


class WriteServiceChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api = _WriteApi()
        self.service = PageWrites(self.api, _Links(self.api), "sample-user", "https://confluence.example.test/wiki")
        exports = tempfile.TemporaryDirectory()
        self.addCleanup(exports.cleanup)
        self.exports = Path(exports.name)
        roots = patch("confluence_connector.service.writes.exports_root", return_value=self.exports)
        roots.start()
        self.addCleanup(roots.stop)

    async def test_create_page_converts_text_and_uses_parent_space(self) -> None:
        result = await self.service.create_page("New page", "<safe>", format="text", parent_id="100")

        self.assertEqual(result.page_id, "700")
        self.assertEqual(result.status, "current")
        self.assertEqual(self.api.create_calls[0]["space_key"], "SYNTH")
        self.assertEqual(self.api.create_calls[0]["parent_id"], "100")
        self.assertEqual(self.api.create_calls[0]["storage_body"], "<p>&lt;safe&gt;</p>")
        self.assertEqual(self.api.title_checks, [("SYNTH", "New page")])
        self.assertTrue(result.notify_watchers)
        self.assertFalse(result.notification_suppression_supported)
        self.assertIsNotNone(result.notification_note)

    async def test_create_page_accepts_a_personal_space_key_prefix(self) -> None:
        result = await self.service.create_page(
            "Personal page", "body", format="text", space_key="~sample.user@example.test"
        )

        self.assertEqual(result.page_id, "700")
        self.assertEqual(self.api.create_calls[0]["space_key"], "~sample.user@example.test")

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

    async def test_create_requires_explicit_current_status_for_parent_placement(self) -> None:
        self.api.metadata["100"].status = None

        with self.assertRaisesRegex(InvalidRequest, "current published pages only"):
            await self.service.create_page("New", "body", format="text", parent_id="100")

        self.assertEqual(self.api.create_calls, [])

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
            "55", "<p>replacement</p>", expected_version=7, allow_comment_loss=True,
            notify_watchers=False,
        )

        self.assertEqual(self.api.update_calls[0]["next_version"], 8)
        self.assertFalse(self.api.update_calls[0]["notify_watchers"])
        self.assertFalse(result.notify_watchers)
        self.assertTrue(result.notification_suppression_supported)
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

    async def test_update_from_unedited_export_file_sends_the_stored_body_unchanged(self) -> None:
        storage = '<p>a&nbsp;b<br /></p><pre>kept</pre><ac:emoticon ac:name="smile" />'
        self.api.pages["55"] = _page("55", 7, storage)
        path = self._file(rich_export_content("# Synthetic 55\nVersion: 7 of 7", "55", storage))

        result = await self.service.update_page("55", body_file=path)

        self.assertEqual(self.api.update_calls[0]["storage_body"], storage)
        self.assertEqual(self.api.update_calls[0]["next_version"], 8)
        self.assertEqual(result.warnings, [])

    async def test_update_from_edited_export_file_converts_only_new_blocks(self) -> None:
        self.api.pages["55"] = _page("55", 7, "<pre>kept</pre>")
        path = self._file(rich_export_content(
            "# Synthetic 55\nVersion: 7", "55", "<pre>kept</pre><pre><code>added</code></pre>"
        ))

        await self.service.update_page("55", body_file=path, expected_version=7)

        sent = self.api.update_calls[0]["storage_body"]
        self.assertTrue(sent.startswith("<pre>kept</pre><ac:structured-macro ac:name=\"code\">"))

    async def test_update_file_must_match_explicit_page_and_version_before_writing(self) -> None:
        path = self._file(rich_export_content("# Synthetic 55\nVersion: 7", "55", "<p>new</p>"))
        with self.assertRaisesRegex(InvalidRequest, "exported from page 55, not page 56"):
            await self.service.update_page("56", body_file=path)
        with self.assertRaisesRegex(InvalidRequest, "exported at version 7, not expected_version 6"):
            await self.service.update_page("55", body_file=path, expected_version=6)
        self.assertEqual(self.api.content_calls, [])
        self.assertEqual(self.api.update_calls, [])

    async def test_update_file_from_an_older_version_is_a_conflict(self) -> None:
        path = self._file(rich_export_content("# Synthetic 55\nVersion: 6", "55", "<p>new</p>"))
        with self.assertRaises(Conflict):
            await self.service.update_page("55", body_file=path)
        self.assertEqual(self.api.update_calls, [])

    async def test_update_refuses_files_without_a_header_and_ambiguous_body_sources(self) -> None:
        plain = self._file("<p>no header</p>")
        exported = self._file(rich_export_content("# Synthetic 55\nVersion: 7", "55", "<p>new</p>"))
        cases = [
            ({"page": "55", "body_file": plain}, "header comment"),
            ({"page": "55", "body": "<p>x</p>", "expected_version": 7, "body_file": exported}, "exactly one"),
            ({"page": "55", "body_file": exported, "format": "text"}, "omit format"),
            ({"page": "55", "body": "<p>x</p>"}, "expected_version is required"),
        ]
        for kwargs, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(InvalidRequest, message):
                await self.service.update_page(**kwargs)
        self.assertEqual(self.api.update_calls, [])

    async def test_create_from_plain_file_converts_pre_and_from_export_keeps_its_storage(self) -> None:
        plain = self._file("<pre>new code</pre>")
        await self.service.create_page("From plain", body_file=plain, space_key="SYNTH")
        self.assertIn('ac:name="code"', self.api.create_calls[0]["storage_body"])

        exported = self._file(rich_export_content("# Source\nVersion: 3", "88", "<pre>preformatted</pre>"))
        await self.service.create_page("From export", body_file=exported, space_key="SYNTH")
        self.assertEqual(self.api.create_calls[1]["storage_body"], "<pre>preformatted</pre>")

    def _file(self, content: str) -> str:
        path = self.exports / f"page-{len(list(self.exports.iterdir()))}.html"
        path.write_text(content, encoding="utf-8", newline="")
        return str(path)

    async def test_body_file_outside_exports_and_client_roots_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere, "secret.html")
            outside.write_text(rich_export_content("# S\nVersion: 7", "55", "<p>x</p>"), encoding="utf-8")
            with self.assertRaisesRegex(InvalidRequest, "exports folder or a folder the client shares"):
                await self.service.update_page("55", body_file=str(outside))
            with self.assertRaisesRegex(InvalidRequest, "exports folder or a folder the client shares"):
                await self.service.create_page("Leak", body_file=str(outside), space_key="SYNTH")
            await self.service.update_page("55", body_file=str(outside), body_file_roots=[Path(elsewhere)])
        self.assertEqual(len(self.api.update_calls), 1)
        self.assertEqual(self.api.create_calls, [])

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

    async def test_draft_cursor_rejects_malformed_or_overridden_scope(self) -> None:
        first = await self.service.list_drafts(limit=1)
        self.assertIsNotNone(first.next_cursor)
        cases = (
            ("added scope", first.next_cursor, "SYNTH", "Pass only cursor"),
            (
                "missing cursor fields",
                encode("list_drafts", start=0, limit=10),
                None,
                "Malformed draft cursor",
            ),
        )
        for name, cursor, space_key, message in cases:
            with self.subTest(case=name), self.assertRaisesRegex(InvalidRequest, message):
                await self.service.list_drafts(cursor=cursor, space_key=space_key)

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

        result = await self.service.publish_draft("301", notify_watchers=False)

        self.assertEqual((result.page_id, result.status, result.version), ("301", "current", 1))
        self.assertEqual(self.api.publish_calls[0]["page_id"], "301")
        self.assertEqual(self.api.publish_calls[0]["storage_body"], "<p>draft</p>")
        self.assertFalse(self.api.publish_calls[0]["notify_watchers"])
        self.assertFalse(result.notify_watchers)
        self.assertTrue(result.notification_suppression_supported)

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

        result = await self.service.delete_draft("301", notify_watchers=False)

        self.assertTrue(result.deleted)
        self.assertTrue(result.permanent)
        self.assertEqual(self.api.deleted_drafts, ["301"])
        self.assertFalse(result.notify_watchers)
        self.assertFalse(result.notification_suppression_supported)
        self.assertIsNotNone(result.notification_note)

    async def test_delete_draft_does_not_delete_current_page(self) -> None:
        with self.assertRaises(InvalidRequest):
            await self.service.delete_draft("55")
        self.assertEqual(self.api.deleted_drafts, [])

    async def test_label_partial_failures_preserve_confirmed_and_unattempted_changes(self) -> None:
        cases = [
            (
                "upstream failure after add",
                ["old", "another"],
                ["new"],
                ["old", "another"],
                "old",
                Upstream,
                ["new"],
                [],
                ["another"],
                [("add", "55", "new"), ("remove", "55", "old")],
            ),
            (
                "account change after add",
                ["old", "another"],
                ["new"],
                ["old", "another"],
                "old",
                AccountMismatch,
                ["new"],
                [],
                ["another"],
                [("add", "55", "new"), ("remove", "55", "old")],
            ),
            (
                "uncertain failure after add",
                ["old"],
                ["new"],
                ["old"],
                "old",
                UncertainWrite,
                ["new"],
                [],
                [],
                [("add", "55", "new"), ("remove", "55", "old")],
            ),
            (
                "later removal failure",
                ["old", "another"],
                [],
                ["old", "another"],
                "another",
                Upstream,
                [],
                ["old"],
                [],
                [("remove", "55", "old"), ("remove", "55", "another")],
            ),
        ]

        for name, labels, additions, removals, failed_label, error_type, added, removed, pending, calls in cases:
            with self.subTest(case=name):
                api = _WriteApi()
                api.pages["55"].metadata.labels = labels
                api.label_errors[("remove", failed_label)] = error_type("synthetic failure")
                service = PageWrites(api, _Links(api), "sample-user", "https://confluence.example.test/wiki")

                result = await service.set_labels("55", add=additions, remove=removals)

                self.assertEqual(result.added, added)
                self.assertEqual(result.removed, removed)
                self.assertEqual([failure.label for failure in result.failures], [failed_label])
                self.assertEqual([failure.label for failure in result.not_attempted], pending)
                self.assertEqual(api.label_calls, calls)

    async def test_set_labels_skips_noops_and_rejects_overlap(self) -> None:
        result = await self.service.set_labels("55", add=["old"], remove=["missing"], notify_watchers=False)
        self.assertEqual(result.unchanged, ["old", "missing"])
        self.assertFalse(result.notify_watchers)
        self.assertFalse(result.notification_suppression_supported)
        self.assertIsNotNone(result.notification_note)
        self.assertEqual(self.api.label_calls, [])
        with self.assertRaisesRegex(InvalidRequest, "both add and remove"):
            await self.service.set_labels("55", add=["same"], remove=["same"])

    async def test_notify_watchers_must_be_a_boolean_before_remote_reads(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "notify_watchers must be a boolean"):
            await self.service.update_page("55", "body", expected_version=7, notify_watchers=1)
        self.assertEqual(self.api.content_calls, [])

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
