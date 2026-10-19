"""Synthetic checks for Confluence view/storage conversion and block boundaries."""

from __future__ import annotations

from pathlib import Path
import unittest

from confluence_connector.domain.models import PageAttachment
from confluence_connector.service.page_text import html_to_text, render_page_body, storage_top_level_blocks

_FIXTURES = Path(__file__).parent / "fixtures"


class PageTextRenderingChecks(unittest.TestCase):
    def test_view_and_storage_keep_markers_and_typed_source_references(self) -> None:
        rendered = render_page_body(
            (_FIXTURES / "page_text_view.html").read_text(encoding="utf-8"),
            (_FIXTURES / "page_text_storage.xml").read_text(encoding="utf-8"),
            page_id="55",
            attachments=[
                PageAttachment(
                    attachment_id="att-7",
                    filename="diagram.png",
                    media_type="image/png",
                    file_size=1024,
                    download_url="https://confluence.example.test/wiki/download/attachments/55/diagram.png",
                ),
                PageAttachment(attachment_id="att-8", filename="guide.txt", media_type="text/plain"),
            ],
        )
        text = "\n\n".join(block.text for block in rendered.blocks)
        self.assertIn("# Runbook", text)
        self.assertIn("Start **carefully**", text)
        self.assertIn("[the linked page](/wiki/spaces/SYNTH/pages/84/Target)", text)
        self.assertIn("@Sample User", text)
        self.assertNotIn("1.2", text)
        self.assertIn("[x] completed step", text)
        self.assertIn("[ ] pending step", text)
        self.assertIn("> [Info] Keep the service online.", text)
        self.assertIn("> [Note] Record the change.", text)
        self.assertIn("> [Warning] Use caution.", text)
        self.assertIn("> [Tip] Check the logs.", text)
        self.assertIn("> [Deployment] Deployment notes.", text)
        self.assertIn("[Status: Approved]", text)
        self.assertIn("[Expand: More]", text)
        self.assertIn("```python\nprint(1)\n```", text)
        self.assertIn("[Included from: Shared guide]", text)
        self.assertIn("[Included from: Shared excerpt]", text)
        self.assertIn("Resolved included text.", text)
        self.assertIn("Shared excerpt text.", text)
        self.assertIn("[Jira: OPS-7]", text)
        self.assertIn("[Diagram: plantuml]\n```plantuml\n@startuml\nA -> B\n@enduml\n```", text)
        self.assertIn("[Image: diagram.png — System map]", text)
        self.assertIn("[File: guide.txt]", text)
        self.assertIn("[Table: merged cells repeated]", text)
        self.assertIn("| Prepare | • Backup<br>• On call |", text)
        self.assertIn("| Prepare | Ops<br>Primary |", text)
        self.assertIn("| [Image: diagram.png — Cell image] | [Status: Cell status] |", text)
        self.assertIn("[Unrendered macro: mystery]", text)
        self.assertIn("[Unrendered macro: html]", text)
        self.assertNotIn("htmlMacroScript", text)
        self.assertNotIn("Generated contents", text)

        kinds = {reference.kind for reference in rendered.references}
        self.assertTrue({"page", "image", "attachment", "jira", "macro", "inline_comment"} <= kinds)
        page_reference = next(reference for reference in rendered.references if reference.kind == "page")
        self.assertEqual((page_reference.page_id, page_reference.title), ("91", "Shared guide"))
        image_reference = next(reference for reference in rendered.references if reference.kind == "image")
        self.assertEqual((image_reference.attachment_id, image_reference.owner_page_id), ("att-7", "55"))

    def test_nested_table_or_code_cell_uses_minimal_html_fallback(self) -> None:
        rendered = render_page_body(
            "<table><tr><th>Heading</th><td><pre><code>x &lt; y</code></pre></td></tr></table>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["<table><tr><th>Heading</th><td>x &lt; y</td></tr></table>"],
        )

    def test_nested_table_markup_is_sanitized_without_flattening_its_rows(self) -> None:
        rendered = render_page_body(
            "<table><tr><td>Outer<table><tr><td>Inner</td></tr></table></td></tr></table>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["<table><tr><td>Outer<table><tr><td>Inner</td></tr></table></td></tr></table>"],
        )

    def test_merged_cells_do_not_overwrite_a_rowspan(self) -> None:
        rendered = render_page_body(
            "<table><tr><td>A</td><td rowspan='2'>B</td><td>C</td></tr>"
            "<tr><td colspan='2'>X</td></tr></table>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            [
                "[Table: merged cells repeated]",
                "| A | B | C |  |",
                "| --- | --- | --- | --- |",
                "|  | B | X | X |",
            ],
        )

    def test_image_alt_text_does_not_become_a_filename(self) -> None:
        rendered = render_page_body(
            '<img alt="A useful diagram">',
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(rendered.blocks[0].text, "[Image: unknown \u2014 A useful diagram]")
        self.assertIsNone(rendered.references[0].filename)
        self.assertEqual(rendered.references[0].alt_text, "A useful diagram")

    def test_page_link_references_must_stay_on_the_configured_instance(self) -> None:
        rendered = render_page_body(
            '<a href="https://outside.example.test/wiki/spaces/SYNTH/pages/84/Other">outside</a>'
            '<a href="https://confluence.example.test/wiki/spaces/SYNTH/pages/91/Local">local</a>',
            "",
            page_id="55",
            base_url="https://confluence.example.test/wiki",
            attachments=[],
        )
        page_ids = {reference.page_id for reference in rendered.references if reference.kind == "page"}
        self.assertEqual(page_ids, {"91"})

    def test_unwrapped_html_macro_identity_is_recovered_from_storage_source(self) -> None:
        rendered = render_page_body(
            "<div><p>Unique HTML output.</p></div><p>Following paragraph.</p>",
            '<ac:structured-macro ac:name="html"><ac:plain-text-body>&lt;p&gt;Unique HTML output.&lt;/p&gt;</ac:plain-text-body></ac:structured-macro>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["[Unrendered macro: html]", "Following paragraph."],
        )

    def test_storage_blocks_preserve_source_markup_for_rich_continuation(self) -> None:
        source = '<p data-x="1">A &amp; B</p>\n<ac:structured-macro ac:name="code"/><p>C</p>'
        self.assertEqual(
            storage_top_level_blocks(source),
            ['<p data-x="1">A &amp; B</p>', '\n', '<ac:structured-macro ac:name="code"/>', '<p>C</p>'],
        )

        self.assertEqual(storage_top_level_blocks("<!--keep--><p>text</p>"), ["<!--keep-->", "<p>text</p>"])
        top_level_text = "A &amp; B &#169;"
        self.assertEqual("".join(storage_top_level_blocks(top_level_text)), top_level_text)

    def test_comment_html_is_plain_text_and_active_script_content_is_dropped(self) -> None:
        self.assertEqual(html_to_text("<p>First <strong>bold</strong>.</p><script>secret()</script><p>Last</p>"), "First **bold**.\n\nLast")


if __name__ == "__main__":
    unittest.main()
