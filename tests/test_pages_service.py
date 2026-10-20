"""Offline page-read budgets, parse scope, typed references and version-pinned cursors."""

from __future__ import annotations

import asyncio
import unittest

from confluence_connector.domain.errors import AuthenticationRequired, InvalidRequest, NotFound, Upstream
from confluence_connector.domain.models import (
    PageAttachment,
    PageMetadata,
    PageParseScope,
    RemotePageComment,
    RemotePageContent,
    ResolvedPageReference,
    PersonSummary,
)
from confluence_connector.service.pages import PageTextService


class PageTextServiceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_text_read_renders_sections_and_keeps_typed_references(self) -> None:
        api = _ReadApi(
            pages={
                "55": _page(
                    "55",
                    3,
                    '<h1>Guide</h1><p>Body text</p><img data-linked-resource-id="att-1" alt="Map">',
                    '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">OPS-9</ac:parameter></ac:structured-macro>',
                )
            },
            attachments={
                "55": [
                    PageAttachment(
                        attachment_id="att-1",
                        filename="map.png",
                        media_type="image/png",
                        file_size=8,
                        download_url="https://confluence.example.test/download/map.png",
                    )
                ]
            },
            comments={
                "55": [
                    RemotePageComment(
                        comment_id="comment-1",
                        author=PersonSummary(display_name="Sample User"),
                        created_at="2026-10-01T00:00:00Z",
                        body_view="<p>First comment</p>",
                        location="footer",
                    ),
                    RemotePageComment(
                        comment_id="comment-2",
                        parent_comment_id="comment-1",
                        author=PersonSummary(display_name="Another User"),
                        body_view="<p>Reply text</p>",
                        location="inline",
                        resolution="resolved",
                        selected_text="Body text",
                    ),
                ]
            },
        )
        result = await _service(api).get_pages("55")

        self.assertEqual(len(result.pages), 1)
        page = result.pages[0]
        self.assertEqual(page.version, 3)
        self.assertIn("# Guide", page.text)
        self.assertIn("## Attachments", page.text)
        self.assertIn("map.png (image/png, 8 bytes)", page.text)
        self.assertIn("## Comments", page.text)
        self.assertIn("@Sample User", page.text)
        self.assertIn("  - @Another User", page.text)
        self.assertIn('Selected text: "Body text"', page.text)
        self.assertIn("Reply text", page.text)
        self.assertEqual({reference.kind for reference in page.references}, {"image", "jira", "attachment", "comment"})
        self.assertEqual(api.optional_calls, [("attachments", "55"), ("comments", "55")])

    async def test_parse_scope_skips_optional_routes_and_rich_body_stays_unchanged(self) -> None:
        storage = '<p data-kind="stored">A &amp; B</p>\n<ac:structured-macro ac:name="html"/>'
        api = _ReadApi(pages={"55": _page("55", 1, "<p>view</p>", storage)})
        result = await _service(api).get_pages(
            "55",
            format="rich",
            parse_scope=PageParseScope(comments=False, attachments=False, macros_from_storage=False),
            max_chars=400,
        )
        self.assertEqual(result.pages[0].text, storage)
        self.assertIsNotNone(result.pages[0].header)
        self.assertEqual(api.optional_calls, [])

    async def test_oversized_page_cursor_reloads_the_original_version(self) -> None:
        old_page = _page("55", 7, "<p>old one</p><p>old two</p><p>old three</p>", "")
        api = _ReadApi(pages={"55": old_page}, versions={("55", 7): old_page})
        first = await _service(api).get_pages(
            "55",
            max_chars=8,
            parse_scope={"header": False, "comments": False, "attachments": False, "macros_from_storage": False},
        )
        self.assertEqual(first.pages[0].text, "old one")
        self.assertEqual(first.pages[0].version, 7)
        self.assertEqual(first.remaining, ["55"])
        self.assertIsNotNone(first.next_cursor)

        api.pages["55"] = _page("55", 8, "<p>new current content</p>", "")
        second = await _service(api).get_pages(cursor=first.next_cursor)
        self.assertEqual(second.pages[0].text, "old two")
        self.assertEqual(second.pages[0].version, 7)
        self.assertNotIn("new current content", second.pages[0].text)
        self.assertEqual(api.content_calls[-1], ("55", 7))

    async def test_structured_comments_follow_the_selected_text_blocks(self) -> None:
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>Body</p>", "")},
            comments={
                "55": [RemotePageComment(comment_id="comment-1", body_view="<p>Long comment body</p>")]
            },
        )
        first = await _service(api).get_pages(
            "55",
            max_chars=4,
            parse_scope={"header": False, "attachments": False},
        )
        self.assertEqual(first.pages[0].text, "Body")
        self.assertEqual(first.pages[0].comments, [])
        self.assertIsNotNone(first.next_cursor)

        second = await _service(api).get_pages(cursor=first.next_cursor)
        self.assertEqual(second.pages[0].text, "## Comments")
        self.assertEqual(second.pages[0].comments, [])
        self.assertIsNotNone(second.next_cursor)

        third = await _service(api).get_pages(cursor=second.next_cursor)
        self.assertEqual(len(third.pages[0].comments), 1)
        self.assertEqual(third.pages[0].comments[0].body, "Long comment body")

    async def test_comment_page_links_keep_the_configured_context_path(self) -> None:
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>Body</p>", "")},
            comments={
                "55": [
                    RemotePageComment(
                        comment_id="comment-1",
                        body_view='<p>See <a href="/spaces/SYNTH/pages/84/Target">target</a>.</p>',
                    )
                ]
            },
        )
        result = await _service(api).get_pages("55", parse_scope={"header": False, "attachments": False})

        self.assertIn("[target](/wiki/spaces/SYNTH/pages/84/Target)", result.pages[0].comments[0].body)
        self.assertIn("[target](/wiki/spaces/SYNTH/pages/84/Target)", result.pages[0].text)

    async def test_rich_cursor_budgets_comments_without_changing_storage_text(self) -> None:
        storage = "<p>Stored</p>"
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>view</p>", storage)},
            comments={"55": [RemotePageComment(comment_id="comment-1", body_view="<p>Comment</p>")]},
        )
        first = await _service(api).get_pages(
            "55",
            format="rich",
            max_chars=len(storage),
            parse_scope={"header": False, "attachments": False},
        )
        self.assertEqual(first.pages[0].text, storage)
        self.assertEqual(first.pages[0].comments, [])
        self.assertIsNotNone(first.next_cursor)

        second = await _service(api).get_pages(cursor=first.next_cursor)
        self.assertEqual(second.pages[0].text, "")
        self.assertEqual(second.pages[0].comments[0].body, "Comment")

    async def test_whole_pages_fit_before_remaining_pages_are_reported(self) -> None:
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>first</p>", ""), "56": _page("56", 1, "<p>second</p>", "")}
        )
        result = await _service(api).get_pages(
            ["55", "56"],
            max_chars=5,
            parse_scope={"header": False, "comments": False, "attachments": False, "macros_from_storage": False},
        )
        self.assertEqual([page.page_id for page in result.pages], ["55"])
        self.assertEqual(result.pages[0].text, "first")
        self.assertEqual(result.remaining, ["56"])
        self.assertIsNone(result.next_cursor)

    async def test_successful_empty_body_is_returned_as_an_empty_page_record(self) -> None:
        api = _ReadApi(pages={"55": _page("55", 1, "", "")})
        result = await _service(api).get_pages(
            "55",
            parse_scope={"header": False, "comments": False, "attachments": False, "macros_from_storage": False},
        )
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].text, "")
        self.assertEqual(result.remaining, [])
        self.assertEqual(result.failures, [])

    async def test_empty_page_does_not_block_an_oversized_next_page(self) -> None:
        api = _ReadApi(
            pages={
                "55": _page("55", 1, "", ""),
                "56": _page("56", 1, "", "<p>first</p><p>second</p>"),
            }
        )
        result = await _service(api).get_pages(
            ["55", "56"],
            format="rich",
            max_chars=5,
            parse_scope={"header": False, "comments": False, "attachments": False, "macros_from_storage": False},
        )
        self.assertEqual([page.page_id for page in result.pages], ["55", "56"])
        self.assertEqual(result.pages[0].text, "")
        self.assertEqual(result.pages[1].text, "<p>first</p>")
        self.assertIsNotNone(result.next_cursor)
        self.assertEqual(result.failures, [])

    async def test_account_failures_propagate_from_optional_reads(self) -> None:
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>body</p>", "")},
            comment_error=AuthenticationRequired("Token cleared"),
        )
        with self.assertRaises(AuthenticationRequired):
            await _service(api).get_pages("55")

    async def test_optional_section_failure_is_reported_without_losing_the_page(self) -> None:
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>body</p>", "")},
            comment_error=Upstream("Synthetic response failure"),
        )
        result = await _service(api).get_pages("55")
        self.assertEqual(result.pages[0].text.splitlines()[-1], "## Comments")
        self.assertEqual(result.pages[0].notes, ["The comments section could not be read (Upstream)."])

    async def test_reference_resolution_failure_is_returned_per_page(self) -> None:
        api = _ReadApi(pages={"55": _page("55", 1, "<p>body</p>", "")})
        result = await _service(api).get_pages(["55", "invalid"])
        self.assertEqual([page.page_id for page in result.pages], ["55"])
        self.assertEqual(result.failures[0].reference, "invalid")
        self.assertIn("NotFound", result.failures[0].error)

    async def test_body_attachments_and_comments_load_in_parallel(self) -> None:
        api = _ConcurrentReadApi(_page("55", 1, "<p>body</p>", ""))
        result = await _service(api).get_pages("55")
        self.assertEqual([page.page_id for page in result.pages], ["55"])
        self.assertEqual(api.maximum_active, 3)

    async def test_two_pages_load_with_six_requests_and_return_in_input_order(self) -> None:
        api = _ConcurrentReadApi(
            {
                "55": _page("55", 1, "<p>first</p>", ""),
                "56": _page("56", 1, "<p>second</p>", ""),
            }
        )
        result = await _service(api).get_pages(["55", "56"])
        self.assertEqual(api.maximum_active, 6)
        self.assertTrue(api.second_page_completed.is_set())
        self.assertEqual([page.page_id for page in result.pages], ["55", "56"])
        self.assertIn("first", result.pages[0].text)
        self.assertIn("second", result.pages[1].text)

    async def test_invalid_public_parameters_fail_before_page_reads(self) -> None:
        api = _ReadApi(pages={"55": _page("55", 1, "<p>body</p>", "")})
        service = _service(api)
        for kwargs in (
            {"pages": []},
            {"pages": ["55"] * 21},
            {"pages": "55", "format": "markdown"},
            {"pages": "55", "max_chars": 400_001},
            {"pages": "55", "parse_scope": {"unknown": True}},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidRequest):
                await service.get_pages(**kwargs)  # type: ignore[arg-type]
        self.assertEqual(api.content_calls, [])


def _service(api: _ReadApi) -> PageTextService:
    return PageTextService(api, _Links(), "sample-user", "https://confluence.example.test/wiki")  # type: ignore[arg-type]


def _page(page_id: str, version: int, view: str, storage: str) -> RemotePageContent:
    return RemotePageContent(
        metadata=PageMetadata(
            page_id=page_id,
            title=f"Synthetic {page_id}",
            content_type="page",
            status="current",
            url=f"https://confluence.example.test/wiki/pages/{page_id}",
            space_key="SYNTH",
            modified_at="2026-10-01T00:00:00Z",
            version=version,
        ),
        view_body=view,
        storage_body=storage,
        version=version,
        total_versions=version,
    )


class _Links:
    async def resolve(self, reference: str, *, expected_username: str) -> ResolvedPageReference:
        if reference == "invalid":
            raise NotFound("Synthetic page unavailable")
        return ResolvedPageReference(page_id=reference, version=None)


class _ReadApi:
    def __init__(
        self,
        *,
        pages: dict[str, RemotePageContent],
        versions: dict[tuple[str, int], RemotePageContent] | None = None,
        attachments: dict[str, list[PageAttachment]] | None = None,
        comments: dict[str, list[RemotePageComment]] | None = None,
        comment_error: Exception | None = None,
    ) -> None:
        self.pages = pages
        self.versions = versions or {}
        self.attachments = attachments or {}
        self.comments = comments or {}
        self.comment_error = comment_error
        self.content_calls: list[tuple[str, int | None]] = []
        self.optional_calls: list[tuple[str, str]] = []

    async def get_page_content(
        self,
        page_id: str,
        *,
        version: int | None = None,
        include_storage: bool = True,
        expected_username: str,
    ) -> RemotePageContent:
        self.content_calls.append((page_id, version))
        if version is not None:
            return self.versions.get((page_id, version)) or self.pages[page_id]
        return self.pages[page_id]

    async def list_page_attachments(self, page_id: str, *, expected_username: str) -> list[PageAttachment]:
        self.optional_calls.append(("attachments", page_id))
        return self.attachments.get(page_id, [])

    async def list_page_comments(self, page_id: str, *, expected_username: str) -> list[RemotePageComment]:
        self.optional_calls.append(("comments", page_id))
        if self.comment_error is not None:
            raise self.comment_error
        return self.comments.get(page_id, [])


class _ConcurrentReadApi(_ReadApi):
    def __init__(self, pages: RemotePageContent | dict[str, RemotePageContent]) -> None:
        page_map = {pages.metadata.page_id: pages} if isinstance(pages, RemotePageContent) else pages
        super().__init__(pages=page_map)
        self._all_started = asyncio.Event()
        self.second_page_completed = asyncio.Event()
        self._active = 0
        self._started = 0
        self._completed: dict[str, int] = {}
        self.maximum_active = 0
        self._expected = 3 * len(page_map)
        self._page_ids = list(page_map)

    async def _start_request(self, page_id: str) -> None:
        self._active += 1
        self._started += 1
        self.maximum_active = max(self.maximum_active, self._active)
        if self._started == self._expected:
            self._all_started.set()
        await asyncio.wait_for(self._all_started.wait(), timeout=1)
        if len(self._page_ids) > 1 and page_id == self._page_ids[0]:
            await asyncio.sleep(0.02)

    def _finish_request(self, page_id: str) -> None:
        self._active -= 1
        self._completed[page_id] = self._completed.get(page_id, 0) + 1
        if len(self._page_ids) > 1 and page_id == self._page_ids[1] and self._completed[page_id] == 3:
            self.second_page_completed.set()

    async def get_page_content(
        self,
        page_id: str,
        *,
        version: int | None = None,
        include_storage: bool = True,
        expected_username: str,
    ) -> RemotePageContent:
        await self._start_request(page_id)
        try:
            self.content_calls.append((page_id, version))
            return self.pages[page_id]
        finally:
            self._finish_request(page_id)

    async def list_page_attachments(self, page_id: str, *, expected_username: str) -> list[PageAttachment]:
        await self._start_request(page_id)
        try:
            self.optional_calls.append(("attachments", page_id))
            return []
        finally:
            self._finish_request(page_id)

    async def list_page_comments(self, page_id: str, *, expected_username: str) -> list[RemotePageComment]:
        await self._start_request(page_id)
        try:
            self.optional_calls.append(("comments", page_id))
            return []
        finally:
            self._finish_request(page_id)


if __name__ == "__main__":
    unittest.main()
