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
            "<table><tr><th>Heading</th><td><pre><code>x &lt; y</code></pre></td>"
            "<td><p>First paragraph</p><p>Second paragraph</p></td></tr></table>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            [
                "<table><tr><th>Heading</th><td>x &lt; y</td>"
                "<td><p>First paragraph</p><p>Second paragraph</p></td></tr></table>"
            ],
        )

    def test_complex_table_fallback_preserves_image_and_macro_markers(self) -> None:
        rendered = render_page_body(
            '<table><tr><td><table><tr><td>Inner</td></tr></table></td>'
            '<td><img data-linked-resource-id="att-9" alt="Plot"></td>'
            '<td><span data-macro-name="jira">Issue</span></td>'
            '<td><a href="/spaces/SYNTH/pages/84/Target">Target</a></td></tr></table>',
            '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">OPS-5</ac:parameter>'
            '</ac:structured-macro>',
            page_id="55",
            base_url="https://confluence.example.test/wiki",
            attachments=[
                PageAttachment(
                    attachment_id="att-9",
                    filename="plot.png",
                    media_type="image/png",
                    file_size=5,
                )
            ],
        )

        self.assertEqual(
            [block.text for block in rendered.blocks],
            [
                "<table><tr><td><table><tr><td>Inner</td></tr></table></td>"
                "<td>[Image: plot.png — Plot]</td><td>[Jira: OPS-5]</td>"
                "<td>[Target](/wiki/spaces/SYNTH/pages/84/Target)</td></tr></table>"
            ],
        )
        self.assertEqual({reference.kind for reference in rendered.references}, {"image", "jira", "page"})
        page_reference = next(reference for reference in rendered.references if reference.kind == "page")
        self.assertEqual(
            (page_reference.page_id, page_reference.url),
            ("84", "/wiki/spaces/SYNTH/pages/84/Target"),
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

    def test_context_relative_page_links_keep_the_configured_context_path(self) -> None:
        rendered = render_page_body(
            '<p><a href="/spaces/SYNTH/pages/84/Target?source=body#details">Target</a></p>',
            "",
            page_id="55",
            base_url="https://confluence.example.test/wiki",
            attachments=[],
        )
        target = "/wiki/spaces/SYNTH/pages/84/Target?source=body#details"
        self.assertEqual(rendered.blocks[0].text, f"[Target]({target})")
        self.assertEqual(rendered.references[0].url, target)

    def test_generic_container_keeps_direct_inline_children_in_one_block(self) -> None:
        rendered = render_page_body(
            "<div>Hello <span>world</span>!</div>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual([block.text for block in rendered.blocks], ["Hello world!"])

    def test_generic_container_splits_inline_runs_around_block_children(self) -> None:
        rendered = render_page_body(
            "<div>Before <span>inline</span><p>Block paragraph</p> after.</div>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["Before inline", "Block paragraph", "after."],
        )

    def test_blockquote_keeps_inline_children_together_and_formats_them(self) -> None:
        rendered = render_page_body(
            "<blockquote>Hello <strong>world</strong>!<p>Next paragraph.</p></blockquote>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["> Hello **world**!", "> Next paragraph."],
        )

    def test_viewpage_query_link_keeps_its_typed_page_reference(self) -> None:
        rendered = render_page_body(
            '<p><a href="/pages/viewpage.action?pageId=123">Target</a></p>',
            "",
            page_id="55",
            base_url="https://confluence.example.test/wiki",
            attachments=[],
        )
        page_references = [reference for reference in rendered.references if reference.kind == "page"]
        self.assertEqual(len(page_references), 1)
        self.assertEqual(page_references[0].page_id, "123")

    def test_page_resource_anchor_keeps_its_label_and_page_reference(self) -> None:
        rendered = render_page_body(
            '<p><a data-linked-resource-id="123" data-linked-resource-type="page" '
            'href="/spaces/SYNTH/pages/Target">Target page</a></p>',
            "",
            page_id="55",
            base_url="https://confluence.example.test/wiki",
            attachments=[],
        )

        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["[Target page](/wiki/spaces/SYNTH/pages/Target)"],
        )
        self.assertEqual(
            [(reference.kind, reference.page_id) for reference in rendered.references],
            [("page", "123")],
        )

    def test_expand_macro_groups_direct_inline_children(self) -> None:
        rendered = render_page_body(
            '<div data-macro-name="expand">Hello <strong>world</strong>!'
            "<p>Next paragraph.</p></div>",
            "",
            page_id="55",
            attachments=[],
        )

        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["[Expand: untitled]", "Hello **world**!", "Next paragraph."],
        )

    def test_unmatched_html_macro_does_not_claim_matching_visible_text(self) -> None:
        rendered = render_page_body(
            "<p>Unique HTML output.</p><p>Following paragraph.</p>",
            '<ac:structured-macro ac:name="html"><ac:plain-text-body>&lt;p&gt;Unique HTML output.&lt;/p&gt;</ac:plain-text-body></ac:structured-macro>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["Unique HTML output.", "Following paragraph.", "[Unrendered macro: html]"],
        )

    def test_authoritative_html_macro_wrapper_keeps_its_marker_inline(self) -> None:
        rendered = render_page_body(
            "<p>Before</p><div data-macro-name='html'><script>rendered output</script></div><p>After</p>",
            '<ac:structured-macro ac:name="html"><ac:plain-text-body>source</ac:plain-text-body></ac:structured-macro>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["Before", "[Unrendered macro: html]", "After"],
        )

    def test_adjacent_identical_blocks_keep_their_order_and_multiplicity(self) -> None:
        rendered = render_page_body(
            "<p>Repeat</p><p>Repeat</p><ul><li>Same item</li><li>Same item</li></ul>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["Repeat", "Repeat", "• Same item", "• Same item"],
        )

    def test_nested_macro_fields_stay_with_their_source_macro(self) -> None:
        rendered = render_page_body(
            '<div data-macro-name="panel">Panel text '
            '<span data-macro-name="status">Approved</span></div>',
            '<ac:structured-macro ac:name="panel"><ac:parameter ac:name="title">Deployment</ac:parameter>'
            '<ac:rich-text-body><p>Panel text</p><ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">Approved</ac:parameter></ac:structured-macro>'
            '</ac:rich-text-body></ac:structured-macro>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["> [Deployment] Panel text [Status: Approved]"],
        )

    def test_unrecognized_macro_keeps_child_block_boundaries(self) -> None:
        rendered = render_page_body(
            '<div data-macro-name="custom"><p>First paragraph.</p><p>Second paragraph.</p></div>',
            "",
            page_id="55",
            attachments=[],
            macros_from_storage=False,
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["First paragraph.", "Second paragraph."],
        )

    def test_storage_macro_cdata_bodies_are_preserved(self) -> None:
        rendered = render_page_body(
            '<div data-macro-name="plantuml"></div><div data-macro-name="code"></div>',
            '<ac:structured-macro ac:name="plantuml"><ac:plain-text-body><![CDATA['
            '@startuml\nA -> B\n@enduml]]></ac:plain-text-body></ac:structured-macro>'
            '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</ac:parameter>'
            '<ac:plain-text-body><![CDATA[if value < 2:\n    print(value)]]></ac:plain-text-body>'
            '</ac:structured-macro>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            [
                "[Diagram: plantuml]\n```plantuml\n@startuml\nA -> B\n@enduml\n```",
                "```python\nif value < 2:\n    print(value)\n```",
            ],
        )

    def test_panel_renders_child_blocks_and_list_markers(self) -> None:
        rendered = render_page_body(
            '<div data-macro-name="info"><p>First paragraph.</p><p>Second paragraph.</p>'
            '<ul><li>Alpha</li><li>Beta</li></ul></div>',
            '<ac:structured-macro ac:name="info"/>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            [
                "> [Info] First paragraph.",
                "> Second paragraph.",
                "> • Alpha",
                "> • Beta",
            ],
        )
        self.assertEqual(
            "".join(
                (block.separator_before if index else "") + block.text
                for index, block in enumerate(rendered.blocks)
            ),
            "> [Info] First paragraph.\n>\n> Second paragraph.\n>\n> • Alpha\n> • Beta",
        )

    def test_ordered_lists_honor_start_and_item_value_attributes(self) -> None:
        rendered = render_page_body(
            '<ol start="4"><li>Fourth</li><li value="9">Ninth</li><li>Tenth</li></ol>'
            '<ol start="-2"><li>Negative</li></ol>',
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["4. Fourth", "9. Ninth", "10. Tenth", "-2. Negative"],
        )

    def test_ordered_lists_in_table_cells_honor_start_and_item_values(self) -> None:
        rendered = render_page_body(
            "<table><tr><td><ol start='4'><li>Fourth</li><li value='9'>Ninth</li>"
            "<li>Tenth</li></ol></td></tr></table>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["| 4. Fourth<br>9. Ninth<br>10. Tenth |", "| --- |"],
        )

    def test_list_items_preserve_multiple_paragraph_boundaries(self) -> None:
        rendered = render_page_body(
            "<ol><li><p>First paragraph.</p><p>Second paragraph.</p></li></ol>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["1. First paragraph.\n\n   Second paragraph."],
        )

    def test_nested_list_indentation_survives_block_cleanup(self) -> None:
        rendered = render_page_body(
            "<ul><li>Parent<ul><li><p>Child first.</p><p>Child second.</p></li></ul></li></ul>",
            "",
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["• Parent", "  • Child first.\n\n    Child second."],
        )

    def test_nested_list_keeps_explicit_line_break_in_one_block(self) -> None:
        rendered = render_page_body(
            "<ul><li>Parent<ul><li>Child first<br>Child second</li></ul></li></ul>",
            "",
            page_id="55",
            attachments=[],
        )

        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["• Parent", "  • Child first\n    Child second"],
        )

    def test_list_item_preserves_parent_content_order_around_nested_list(self) -> None:
        rendered = render_page_body(
            '<ul><li><p>Before</p><ul><li><a href="/spaces/SYNTH/pages/84/Target">Nested</a></li>'
            '</ul><p>After</p></li></ul>',
            "",
            page_id="55",
            base_url="https://confluence.example.test/wiki",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            [
                "• Before",
                "  • [Nested](/wiki/spaces/SYNTH/pages/84/Target)",
                "  After",
            ],
        )
        self.assertEqual(rendered.blocks[1].references[0].page_id, "84")

    def test_macros_inside_resolved_includes_do_not_consume_local_metadata(self) -> None:
        rendered = render_page_body(
            '<div data-macro-name="include"><div data-macro-name="jira">Included issue</div></div>'
            '<div data-macro-name="jira">Local issue</div>',
            '<ac:structured-macro ac:name="include"><ri:page ri:content-id="91" '
            'ri:content-title="Shared"/></ac:structured-macro><ac:structured-macro ac:name="jira">'
            '<ac:parameter ac:name="key">OPS-9</ac:parameter></ac:structured-macro>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["[Included from: Shared]", "Included issue", "[Jira: OPS-9]"],
        )
        self.assertEqual(rendered.blocks[-1].references[0].key, "OPS-9")
        include_reference = next(
            reference for reference in rendered.references if reference.kind == "page"
        )
        self.assertEqual((include_reference.page_id, include_reference.owner_page_id), ("91", "55"))

    def test_explicit_line_breaks_survive_rendering_and_comment_conversion(self) -> None:
        rendered = render_page_body(
            "<p>First<br>Second</p><div data-macro-name='panel'>Top<br>Bottom</div>",
            '<ac:structured-macro ac:name="panel"/>',
            page_id="55",
            attachments=[],
        )
        self.assertEqual(
            [block.text for block in rendered.blocks],
            ["First\nSecond", "> [Panel] Top\n> Bottom"],
        )
        self.assertEqual(html_to_text("<p>First<br>Second</p>"), "First\nSecond")

    def test_storage_blocks_preserve_source_markup_for_rich_continuation(self) -> None:
        source = '<p data-x="1">A &amp; B</p>\n<ac:structured-macro ac:name="code"/><p>C</p>'
        self.assertEqual(
            storage_top_level_blocks(source),
            ['<p data-x="1">A &amp; B</p>\n', '<ac:structured-macro ac:name="code"/>', '<p>C</p>'],
        )

        self.assertEqual(storage_top_level_blocks("<!--keep--><p>text</p>"), ["<!--keep-->", "<p>text</p>"])
        top_level_text = "A &amp; B &#169;"
        self.assertEqual(storage_top_level_blocks(top_level_text), [top_level_text])

    def test_comment_html_is_plain_text_and_active_script_content_is_dropped(self) -> None:
        self.assertEqual(html_to_text("<p>First <strong>bold</strong>.</p><script>secret()</script><p>Last</p>"), "First **bold**.\n\nLast")


if __name__ == "__main__":
    unittest.main()
