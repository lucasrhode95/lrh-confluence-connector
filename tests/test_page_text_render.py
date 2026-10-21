"""Page text conversion: one realistic synthetic page with a reviewed golden output, plus the
Confluence-specific rules of requirements D3. Generic HTML rendering belongs to markdownify and is not
re-tested here rule by rule."""

from __future__ import annotations

import unittest
from pathlib import Path

from confluence_connector.domain.models import PageAttachment
from confluence_connector.service.page_text import parse_storage, render_page_body

_FIXTURES = Path(__file__).parent / "fixtures" / "page_text"
_BASE_URL = "https://wiki.example.test/wiki"
_ATTACHMENTS = [
    PageAttachment(attachment_id="200", filename="flow.png", media_type="image/png", file_size=2048),
    PageAttachment(
        attachment_id="201", filename="checklist.pptx", media_type="application/vnd.ms-powerpoint", file_size=4096
    ),
]


def _render(view: str, storage: str = "", *, attachments: list[PageAttachment] | None = None):
    return render_page_body(
        view, storage, page_id="100", attachments=attachments or [], base_url=_BASE_URL
    )


def _text(view: str, storage: str = "") -> str:
    return "\n\n".join(block.text for block in _render(view, storage).blocks)


class PageTextRenderingChecks(unittest.TestCase):
    def test_runbook_matches_the_reviewed_golden_output(self) -> None:
        rendered = _render(
            (_FIXTURES / "runbook.view.html").read_text(encoding="utf-8"),
            (_FIXTURES / "runbook.storage.xml").read_text(encoding="utf-8"),
            attachments=_ATTACHMENTS,
        )
        text = "\n\n".join(block.text for block in rendered.blocks) + "\n"
        self.assertEqual(text, (_FIXTURES / "runbook.expected.md").read_text(encoding="utf-8"))

    def test_references_come_from_source_structure_not_from_markers(self) -> None:
        rendered = _render(
            (_FIXTURES / "runbook.view.html").read_text(encoding="utf-8"),
            (_FIXTURES / "runbook.storage.xml").read_text(encoding="utf-8"),
            attachments=_ATTACHMENTS,
        )
        by_kind: dict[str, list] = {}
        for reference in rendered.references:
            by_kind.setdefault(reference.kind, []).append(reference)
        self.assertEqual({item.key for item in by_kind["jira"]}, {"OPS-7"})
        self.assertIn("91", {item.page_id for item in by_kind["page"]})
        image = by_kind["image"][0]
        self.assertEqual((image.attachment_id, image.filename, image.size), ("200", "flow.png", 2048))
        self.assertIn("201", {item.attachment_id for item in by_kind["attachment"]})
        self.assertEqual([item.comment_id for item in by_kind["inline_comment"]], ["inline-4"])
        self.assertEqual({item.macro_name for item in by_kind["macro"]}, {"plantuml", "mystery-plugin", "html"})
        self.assertTrue(all(item.owner_page_id in {"100", None} for item in rendered.references))

    def test_macros_inside_an_include_do_not_consume_local_macro_sources(self) -> None:
        view = (
            '<div data-macro-name="include"><span data-macro-name="jira">error</span></div>'
            '<span data-macro-name="jira">error</span>'
        )
        storage = (
            '<ac:structured-macro ac:name="include"><ac:parameter ac:name=""><ac:link>'
            '<ri:page ri:content-title="Shared" ri:content-id="9" /></ac:link></ac:parameter>'
            "</ac:structured-macro>"
            '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">OPS-1</ac:parameter>'
            "</ac:structured-macro>"
        )
        self.assertEqual(
            _text(view, storage),
            "[Included from: Shared]\n\nerror\n\n[Jira: OPS-1]",
        )

    def test_links_are_normalized_to_the_configured_instance_only(self) -> None:
        rendered = _render(
            '<p><a href="/spaces/S/pages/5/A">context-relative</a> '
            '<a href="/wiki/pages/viewpage.action?pageId=6">with context</a> '
            '<a href="https://other.example.test/wiki/spaces/S/pages/7/B">other host</a></p>'
        )
        text = rendered.blocks[0].text
        self.assertIn("(https://wiki.example.test/wiki/spaces/S/pages/5/A)", text)
        self.assertIn("(https://wiki.example.test/wiki/pages/viewpage.action?pageId=6)", text)
        self.assertIn("(https://other.example.test/wiki/spaces/S/pages/7/B)", text)
        self.assertEqual({item.page_id for item in rendered.references if item.kind == "page"}, {"5", "6"})

    def test_merged_and_nested_tables_keep_minimal_html(self) -> None:
        merged = _text(
            '<table><tr><th>A</th><th>B</th></tr><tr><td colspan="2">wide</td></tr>'
            '<tr><td rowspan="2">tall</td><td>1</td></tr><tr><td>2</td></tr></table>'
        )
        self.assertEqual(
            merged,
            '<table><tr><th>A</th><th>B</th></tr><tr><td colspan="2">wide</td></tr>'
            '<tr><td rowspan="2">tall</td><td>1</td></tr><tr><td>2</td></tr></table>',
        )
        complex_table = _text(
            '<table class="confluenceTable"><tr><td class="confluenceTd" style="color:red">x</td>'
            '<td><table><tr><td colspan="2">inner</td></tr></table></td></tr></table>'
        )
        self.assertEqual(
            complex_table,
            '<table><tr><td>x</td><td><table><tr><td colspan="2">inner</td></tr></table></td></tr></table>',
        )

    def test_complex_table_preserves_links_and_is_one_budget_block(self) -> None:
        rendered = _render('<table class="layout"><tr><td><pre>first\n\nsecond</pre>'
            '<a href="https://example.test/guide" style="color:red">Guide</a>'
            '<a href="javascript:bad()">Unsafe</a><a href="https://[broken">Broken</a></td></tr></table>')
        self.assertEqual(len(rendered.blocks), 1)
        text = rendered.blocks[0].text
        self.assertIn('<a href="https://example.test/guide">Guide</a>', text)
        self.assertIn('first\n\nsecond', text)
        self.assertNotIn('javascript:', text)
        self.assertNotIn('style=', text)
        self.assertIn('<a>Broken</a>', text)

    def test_active_content_is_dropped_and_the_html_macro_is_marked(self) -> None:
        text = _text(
            "<p>Visible</p><script>steal()</script><style>p{}</style>",
            '<ac:structured-macro ac:name="html"><ac:plain-text-body><![CDATA[<script>steal()</script>]]>'
            "</ac:plain-text-body></ac:structured-macro>",
        )
        self.assertEqual(text, "Visible\n\n[Unrendered macro: html]")

    def test_identifiers_and_symbols_are_not_escaped(self) -> None:
        self.assertEqual(
            _text("<p>Use table_name.field and *args with 1. item</p>"),
            "Use table_name.field and *args with 1. item",
        )

    def test_storage_blocks_split_top_level_elements_verbatim(self) -> None:
        storage = (_FIXTURES / "runbook.storage.xml").read_text(encoding="utf-8")
        blocks = parse_storage(storage, page_id="100", include_top_level_blocks=True).top_level_blocks
        self.assertEqual("".join(blocks), storage)
        self.assertTrue(blocks[0].startswith("<h1>Overview</h1>"))
        code = next(block for block in blocks if 'ac:name="code"' in block)
        self.assertIn('<![CDATA[def deploy(target_env):\n    return target_env < 2 and "ok"]]>', code)
        self.assertTrue(all(block.strip() for block in blocks))


if __name__ == "__main__":
    unittest.main()
