"""Offline page-read budgets, parse scope, typed references and version-pinned cursors."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from confluence_connector.domain.errors import AuthenticationRequired, InvalidRequest, NotFound, Upstream
from confluence_connector.domain.models import (
    PageAttachment,
    PageMetadata,
    PageParseScope,
    RemotePageComment,
    RemotePageContent,
    RemotePageStorage,
    ResolvedPageReference,
    PersonSummary,
)
from confluence_connector.service.pages import PageTextService


class PageTextServiceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_macros_from_included_pages_are_typed_owned_and_read_once(self) -> None:
        host = _page(
            "55",
            1,
            '<div data-macro-name="include"><span data-macro-name="status">Ready</span></div>',
            _include("90") + _include("90"),
        )
        included = _page(
            "90",
            2,
            "<p>Included</p>",
            '<ac:structured-macro ac:name="status"/>'
            '<ac:structured-macro ac:name="expand"/>'
            '<ac:structured-macro ac:name="html"/>'
            + _include("91"),
        )
        nested = _page("91", 3, "<p>Nested</p>", '<ac:structured-macro ac:name="plantuml"/>')
        api = _ReadApi(pages={"55": host, "90": included, "91": nested})

        result = await _service(api).get_pages(
            "55", parse_scope={"comments": False, "attachments": False}
        )

        macro_references = [item for item in result.pages[0].references if item.kind == "macro"]
        self.assertEqual(
            {(item.macro_name, item.owner_page_id) for item in macro_references},
            {("status", "90"), ("expand", "90"), ("html", "90"), ("plantuml", "91")},
        )
        self.assertEqual(api.storage_calls, ["90", "91"])
        self.assertEqual(api.content_calls, [("55", None)])

    async def test_excerpt_include_reports_only_macros_inside_the_excerpt(self) -> None:
        host = _page("55", 1, "<p>Host</p>", _include("90", macro="excerpt-include"))
        included = _page(
            "90",
            2,
            "<p>Included</p>",
            '<ac:structured-macro ac:name="excerpt"><ac:rich-text-body>'
            '<ac:structured-macro ac:name="status"/></ac:rich-text-body></ac:structured-macro>'
            '<ac:structured-macro ac:name="html"/>',
        )
        api = _ReadApi(pages={"55": host, "90": included})

        result = await _service(api).get_pages(
            "55", parse_scope={"comments": False, "attachments": False}
        )

        included_macros = {
            item.macro_name for item in result.pages[0].references
            if item.kind == "macro" and item.owner_page_id == "90"
        }
        self.assertEqual(included_macros, {"status"})

    async def test_continuation_does_not_read_included_pages_again(self) -> None:
        host = _page(
            "55",
            1,
            "".join(f"<p>Paragraph {index} with enough text to split.</p>" for index in range(6)),
            _include("90"),
        )
        included = _page("90", 2, "<p>Included</p>", '<ac:structured-macro ac:name="status"/>')
        api = _ReadApi(pages={"55": host, "90": included})
        service = _service(api)

        first = await service.get_pages(
            "55", max_chars=80, parse_scope={"comments": False, "attachments": False, "header": False}
        )
        self.assertIsNotNone(first.next_cursor)
        second = await service.get_pages(cursor=first.next_cursor)

        self.assertEqual(api.storage_calls, ["90"])
        self.assertIn("Included-page macro references were returned with the first part of this page.", second.notes)

    async def test_include_title_uses_host_space_and_reports_cycles_and_read_failures(self) -> None:
        host = _page("55", 1, "<p>Host</p>", _include(title="Shared page"))
        included = _page("90", 2, "<p>Shared</p>", _include("55") + _include("92"))
        api = _ReadApi(
            pages={"55": host, "90": included},
            title_matches={("SYNTH", "Shared page"): "90"},
        )

        result = await _service(api).get_pages(
            "55", parse_scope={"comments": False, "attachments": False}
        )

        self.assertIn(("SYNTH", "Shared page"), api.title_lookups)
        self.assertIn("An include cycle was detected; recursion stopped.", result.pages[0].notes)
        self.assertTrue(any("included page could not be read (NotFound)" in note for note in result.pages[0].notes))
        self.assertEqual(api.content_calls, [("55", None)])

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

    async def test_rich_read_parses_storage_once_and_skips_markdown_conversion(self) -> None:
        storage = (
            "<p>Stored</p>"
            '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">SYN-7</ac:parameter>'
            "</ac:structured-macro>"
            '<ri:attachment ri:content-id="att-1" ri:filename="guide.pdf"/>'
        )
        api = _ReadApi(pages={"55": _page("55", 1, "<p>Rendered view</p>", storage)})

        with (
            patch(
                "confluence_connector.service.page_text.BeautifulSoup",
                wraps=BeautifulSoup,
            ) as parse_html,
            patch(
                "confluence_connector.service.page_text._markdown_blocks",
                side_effect=AssertionError("rich reads must not convert Markdown"),
            ) as markdown,
        ):
            result = await _service(api).get_pages(
                "55",
                format="rich",
                parse_scope={"header": False, "comments": False, "attachments": False},
            )

        self.assertEqual(result.pages[0].text, storage)
        self.assertEqual(
            {reference.kind for reference in result.pages[0].references},
            {"attachment", "jira"},
        )
        jira_reference = next(
            reference for reference in result.pages[0].references if reference.kind == "jira"
        )
        self.assertEqual(jira_reference.key, "SYN-7")
        self.assertEqual(
            sum(call.args[0] == storage for call in parse_html.call_args_list), 1
        )
        markdown.assert_not_called()

    async def test_oversized_page_cursor_reloads_the_original_version(self) -> None:
        old_page = _page("55", 7, "<p>old one</p><p>old two</p><p>old three</p>", "")
        api = _ReadApi(pages={"55": old_page}, versions={("55", 7): old_page})
        first = await _service(api).get_pages(
            "55",
            max_chars=10,
            parse_scope={"header": False, "comments": False, "attachments": False, "macros_from_storage": False},
        )
        self.assertEqual(first.pages[0].text, "old one")
        self.assertEqual(first.pages[0].version, 7)
        self.assertEqual(first.remaining, ["55"])
        self.assertIsNotNone(first.next_cursor)

        api.pages["55"] = _page("55", 8, "<p>new current content</p>", "")
        second = await _service(api).get_pages(cursor=first.next_cursor)
        self.assertEqual(second.pages[0].text, "\n\nold two")
        self.assertEqual(first.pages[0].text + second.pages[0].text, "old one\n\nold two")
        self.assertEqual(second.pages[0].version, 7)
        self.assertNotIn("new current content", second.pages[0].text)
        self.assertEqual(api.content_calls[-1], ("55", 7))

    async def test_budget_cuts_fall_between_blocks_and_fragments_rejoin(self) -> None:
        page = _page(
            "55",
            4,
            "<p>First paragraph.</p>"
            "<table><tr><td><pre>code</pre></td><td>Row</td></tr></table>"
            '<div data-macro-name="panel"><p>Panel text.</p><p>More panel text.</p></div>',
            "",
        )
        api = _ReadApi(pages={"55": page}, versions={("55", 4): page})
        scope = {"header": False, "attachments": False, "comments": False, "macros_from_storage": False}
        first = await _service(api).get_pages("55", max_chars=len("First paragraph."), parse_scope=scope)

        self.assertEqual(first.pages[0].text, "First paragraph.")
        self.assertIsNotNone(first.next_cursor)
        second = await _service(api).get_pages(cursor=first.next_cursor)
        self.assertTrue(second.pages[0].text.startswith("\n\n<table>"))
        self.assertTrue(any("returned whole" in note for note in second.notes))
        self.assertIsNotNone(second.next_cursor)
        third = await _service(api).get_pages(cursor=second.next_cursor)
        self.assertEqual(third.pages[0].text, "\n\n> [Panel]\n>\n> Panel text.\n>\n> More panel text.")
        whole = await _service(api).get_pages("55", parse_scope=scope)
        self.assertEqual(first.pages[0].text + second.pages[0].text + third.pages[0].text, whole.pages[0].text)

    async def test_pipe_table_rows_remain_adjacent_in_page_text(self) -> None:
        api = _ReadApi(
            pages={
                "55": _page(
                    "55",
                    1,
                    "<table><tr><th>Heading</th><th>Value</th></tr>"
                    "<tr><td>Row</td><td>1</td></tr></table>",
                    "",
                )
            }
        )
        result = await _service(api).get_pages(
            "55",
            parse_scope={
                "header": False,
                "attachments": False,
                "comments": False,
                "macros_from_storage": False,
            },
        )

        self.assertIn("| Heading | Value |\n| --- | --- |\n| Row | 1 |", result.pages[0].text)
        self.assertNotIn("| Heading | Value |\n\n| --- |", result.pages[0].text)

    async def test_attachment_cursor_resumes_at_stable_item_after_insertions(self) -> None:
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>Body</p>", "")},
            attachments={
                "55": [
                    PageAttachment(attachment_id="att-1", filename="first.txt", media_type="text/plain", file_size=1),
                    PageAttachment(attachment_id="att-2", filename="second.txt", media_type="text/plain", file_size=2),
                ]
            },
        )
        first_text = "Body\n\n## Attachments\n\n- first.txt (text/plain, 1 bytes)"
        first = await _service(api).get_pages(
            "55",
            max_chars=len(first_text),
            parse_scope={"header": False, "comments": False, "macros_from_storage": False},
        )
        self.assertEqual(first.pages[0].text, first_text)
        self.assertIsNotNone(first.next_cursor)

        api.attachments["55"].insert(
            1,
            PageAttachment(attachment_id="att-new", filename="inserted.txt", media_type="text/plain"),
        )
        continued = await _service(api).get_pages(cursor=first.next_cursor)
        self.assertEqual(continued.pages[0].text, "\n\n- second.txt (text/plain, 2 bytes)")
        self.assertEqual(
            first.pages[0].text + continued.pages[0].text,
            first_text + "\n\n- second.txt (text/plain, 2 bytes)",
        )
        self.assertNotIn("inserted.txt", continued.pages[0].text)

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
        self.assertEqual(second.pages[0].text, "\n\n## Comments")
        self.assertEqual(second.pages[0].comments, [])
        self.assertIsNotNone(second.next_cursor)

        third = await _service(api).get_pages(cursor=second.next_cursor)
        self.assertEqual(len(third.pages[0].comments), 1)
        self.assertEqual(third.pages[0].comments[0].body, "Long comment body")

    async def test_comment_page_links_keep_the_configured_context_path(self) -> None:
        api = _ReadApi(
            pages={"55": _page("55", 1, "<p>Body</p>", "")},
            attachments={
                "55": [
                    PageAttachment(
                        attachment_id="att-9",
                        filename="plot.png",
                        media_type="image/png",
                        file_size=5,
                        download_url="https://confluence.example.test/wiki/download/plot.png",
                    )
                ]
            },
            comments={
                "55": [
                    RemotePageComment(
                        comment_id="comment-1",
                        body_view=(
                            '<p>See <a href="/spaces/SYNTH/pages/84/Target">target</a>. '
                            '<img data-linked-resource-id="att-9" alt="Plot"> '
                            '<a data-linked-resource-id="att-9">file</a></p>'
                        ),
                    )
                ]
            },
        )
        result = await _service(api).get_pages("55", parse_scope={"header": False})

        self.assertIn("[target](https://confluence.example.test/wiki/spaces/SYNTH/pages/84/Target)", result.pages[0].comments[0].body)
        self.assertIn("[Image: plot.png — Plot]", result.pages[0].comments[0].body)
        self.assertIn("[File: plot.png]", result.pages[0].comments[0].body)
        self.assertIn("[target](https://confluence.example.test/wiki/spaces/SYNTH/pages/84/Target)", result.pages[0].text)
        comment_references = result.pages[0].comments[0].references
        self.assertEqual({reference.kind for reference in comment_references}, {"page", "image", "attachment"})
        self.assertTrue(all(reference.owner_page_id == "55" for reference in comment_references))
        self.assertTrue({reference.kind for reference in result.pages[0].references} >= {"page", "image", "attachment"})

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

    async def test_oversized_generated_cursor_is_rejected(self) -> None:
        long_page_ids = ["8" * 1_000 + f"{index:02}" for index in range(19)]
        page_ids = ["55", *long_page_ids]
        api = _ReadApi(
            pages={page_id: _page(page_id, 1, "<p>first</p><p>second</p>", "") for page_id in page_ids}
        )

        with self.assertRaisesRegex(InvalidRequest, "cursor exceeds the supported size"):
            await _service(api).get_pages(
                page_ids,
                max_chars=len("first"),
                parse_scope={
                    "header": False,
                    "comments": False,
                    "attachments": False,
                    "macros_from_storage": False,
                },
            )

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


def _include(
    page_id: str | None = None,
    *,
    title: str | None = None,
    space_key: str | None = None,
    macro: str = "include",
) -> str:
    target_attributes = []
    if page_id is not None:
        target_attributes.append(f'ri:content-id="{page_id}"')
    if title is not None:
        target_attributes.append(f'ri:content-title="{title}"')
    if space_key is not None:
        target_attributes.append(f'ri:space-key="{space_key}"')
    target = "<ri:page " + " ".join(target_attributes) + "/>" if target_attributes else ""
    return (
        f'<ac:structured-macro ac:name="{macro}"><ac:parameter ac:name="">'
        + target
        + "</ac:parameter></ac:structured-macro>"
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
        title_matches: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.pages = pages
        self.versions = versions or {}
        self.attachments = attachments or {}
        self.comments = comments or {}
        self.comment_error = comment_error
        self.title_matches = title_matches or {}
        self.content_calls: list[tuple[str, int | None]] = []
        self.optional_calls: list[tuple[str, str]] = []
        self.title_lookups: list[tuple[str, str]] = []
        self.storage_calls: list[str] = []

    async def get_page_storage(self, page_id: str, *, expected_username: str) -> RemotePageStorage:
        self.storage_calls.append(page_id)
        try:
            page = self.pages[page_id]
        except KeyError:
            raise NotFound("Synthetic included page unavailable") from None
        return RemotePageStorage(page_id=page_id, space_key=page.metadata.space_key, storage_body=page.storage_body)

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
        try:
            return self.pages[page_id]
        except KeyError:
            raise NotFound("Synthetic included page unavailable") from None

    async def find_page_by_title(
        self, space_key: str, title: str, *, expected_username: str
    ) -> str:
        self.title_lookups.append((space_key, title))
        try:
            return self.title_matches[(space_key, title)]
        except KeyError:
            raise NotFound("Synthetic include target unavailable") from None

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
