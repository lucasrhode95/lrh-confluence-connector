"""Offline checks for D5 rich-input conversion and active-content refusal."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from confluence_connector.domain.errors import InvalidRequest
from confluence_connector.service.write_markup import content_loss, file_to_storage, to_storage


class WriteMarkupChecks(unittest.TestCase):
    def test_text_is_escaped_and_split_into_paragraphs(self) -> None:
        self.assertEqual(
            to_storage("  <first & second>  \nline\n\nthird", "text"),
            "<p>&lt;first &amp; second&gt;  <br/>line</p><p>third</p>",
        )

    def test_rich_markup_is_preserved_except_for_void_element_closure(self) -> None:
        value = '<p data-x="a">&amp;<br><img src="image.png"></p>'
        self.assertEqual(
            to_storage(value, "rich"),
            '<p data-x="a">&amp;<br/><img src="image.png"/></p>',
        )

    def test_html_named_entities_keep_well_formed_markup_converted(self) -> None:
        converted = to_storage(
            '<p>a&nbsp;b&mdash;c<br></p><pre><code class="language-python">x</code></pre>', "rich"
        )
        self.assertIn("<br/>", converted)
        self.assertIn('ac:name="code"', converted)

    def test_malformed_rich_markup_is_passed_to_confluence_without_repair(self) -> None:
        for value in ("<p><strong>unfinished</p>", "<p>first</p><p>second", "<p>&unknownentity; <br></p>"):
            with self.subTest(value=value):
                self.assertEqual(to_storage(value, "rich"), value)

    def test_pre_code_becomes_language_code_macro_and_safely_splits_cdata(self) -> None:
        value = "<pre><code class=\"language-python\">print(&lt;tag&gt;) ]]&gt;</code></pre>"
        self.assertEqual(
            to_storage(value, "rich"),
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body><![CDATA[print(<tag>) ]]]]><![CDATA[>]]></ac:plain-text-body>"
            "</ac:structured-macro>",
        )

    def test_plain_pre_becomes_code_macro_without_language(self) -> None:
        self.assertEqual(
            to_storage("<pre>one &amp; two</pre>", "rich"),
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[one & two]]></ac:plain-text-body>"
            "</ac:structured-macro>",
        )

    def test_details_summary_becomes_expand_and_nested_conversion_runs(self) -> None:
        value = "<details><summary>More &amp; notes</summary><p>Body</p><pre>x</pre></details>"
        self.assertEqual(
            to_storage(value, "rich"),
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">More &amp; notes</ac:parameter>'
            "<ac:rich-text-body><p>Body</p>"
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body><![CDATA[x]]></ac:plain-text-body>"
            "</ac:structured-macro></ac:rich-text-body></ac:structured-macro>",
        )

    def test_active_content_and_javascript_urls_are_refused(self) -> None:
        unsafe_values = (
            "<script>alert(1)</script>",
            "<style>p { display:none }</style>",
            "<iframe src=\"x\"></iframe>",
            "<object data=\"x\"></object>",
            "<embed src=\"x\">",
            "<img onerror=\"run()\">",
            "<a href=\"java&#x73;cript:run()\">run</a>",
            '<ac:structured-macro ac:name="html"></ac:structured-macro>',
        )
        for value in unsafe_values:
            with self.subTest(value=value), self.assertRaises(InvalidRequest):
                to_storage(value, "rich")

    def test_update_loss_detection_reports_anchors_macros_and_attachments(self) -> None:
        before = (
            '<ac:inline-comment-marker ac:ref="comment-1"><p>x</p></ac:inline-comment-marker>'
            '<ac:structured-macro ac:name="jira"></ac:structured-macro>'
            '<ri:attachment ri:filename="guide.pdf"/>'
        )
        has_anchor_loss, warnings = content_loss(before, "<p>replacement</p>")
        self.assertTrue(has_anchor_loss)
        self.assertEqual(
            warnings,
            [
                "1 inline-comment anchor(s) would be detached.",
                "1 jira macro(s) would be removed.",
                "1 attachment reference(s) would be removed.",
            ],
        )

    def test_update_loss_detection_keeps_matching_source_items(self) -> None:
        body = (
            '<ac:inline-comment-marker ac:ref="comment-1">x</ac:inline-comment-marker>'
            '<ac:structured-macro ac:name="panel"></ac:structured-macro>'
            '<ri:attachment ri:filename="guide.pdf"/>'
        )
        self.assertEqual(content_loss(body, body), (False, []))

    def test_macro_target_change_is_reported_even_when_macro_name_remains(self) -> None:
        before = (
            '<ac:structured-macro ac:name="include">'
            '<ri:page ri:content-title="Old page"/>'
            "</ac:structured-macro>"
        )
        replacement = (
            '<ac:structured-macro ac:name="include">'
            '<ri:page ri:content-title="New page"/>'
            "</ac:structured-macro>"
        )
        self.assertEqual(content_loss(before, replacement), (False, ["1 include macro(s) would be removed."]))

    def test_macro_parameter_edit_with_stable_id_reports_changed_not_removed(self) -> None:
        before = (
            '<ac:structured-macro ac:name="status" ac:macro-id="stable-1">'
            '<ac:parameter ac:name="title">Old</ac:parameter>'
            '<ac:parameter ac:name="colour">Red</ac:parameter>'
            "</ac:structured-macro>"
        )
        replacement = (
            '<ac:structured-macro ac:name="status" ac:macro-id="stable-1">'
            '<ac:parameter ac:name="title">New</ac:parameter>'
            '<ac:parameter ac:name="colour">Green</ac:parameter>'
            "</ac:structured-macro>"
        )

        self.assertEqual(content_loss(before, replacement), (False, ["1 status macro(s) changed."]))

    def test_macro_body_edit_with_stable_id_reports_changed(self) -> None:
        before = (
            '<ac:structured-macro ac:name="panel" ac:macro-id="stable-2">'
            "<ac:rich-text-body>Before</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        replacement = (
            '<ac:structured-macro ac:name="panel" ac:macro-id="stable-2">'
            "<ac:rich-text-body>After</ac:rich-text-body>"
            "</ac:structured-macro>"
        )

        self.assertEqual(content_loss(before, replacement), (False, ["1 panel macro(s) changed."]))

    def test_macro_without_id_uses_name_and_parameters_as_fallback_identity(self) -> None:
        before = (
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">Old</ac:parameter>'
            "</ac:structured-macro>"
        )
        replacement = (
            '<ac:structured-macro ac:name="status">'
            '<ac:parameter ac:name="title">New</ac:parameter>'
            "</ac:structured-macro>"
        )

        self.assertEqual(content_loss(before, replacement), (False, ["1 status macro(s) would be removed."]))

    def test_macro_without_id_on_one_side_uses_the_fallback_identity(self) -> None:
        before = (
            '<ac:structured-macro ac:name="panel" ac:macro-id="stable-4">'
            '<ac:parameter ac:name="title">Same</ac:parameter>'
            "<ac:rich-text-body>Before</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        replacement = (
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="title">Same</ac:parameter>'
            "<ac:rich-text-body>After</ac:rich-text-body>"
            "</ac:structured-macro>"
        )

        self.assertEqual(content_loss(before, replacement), (False, ["1 panel macro(s) changed."]))

    def test_macro_id_moved_to_another_name_reports_a_change(self) -> None:
        before = '<ac:structured-macro ac:name="status" ac:macro-id="stable-3"></ac:structured-macro>'
        replacement = '<ac:structured-macro ac:name="panel" ac:macro-id="stable-3"></ac:structured-macro>'

        self.assertEqual(content_loss(before, replacement), (False, ["1 status macro(s) changed to panel."]))

    def test_target_ownership_changes_are_reported(self) -> None:
        cases = [
            ('<ri:attachment ri:filename="guide.pdf"><ri:page ri:content-title="Owner" ri:space-key="A"/></ri:attachment>', 'attachment reference'),
            ('<ac:structured-macro ac:name="include"><ri:page ri:content-title="Guide" ri:space-key="A"/></ac:structured-macro>', 'include macro'),
        ]
        for original, warning in cases:
            with self.subTest(target=warning):
                self.assertEqual(content_loss(original, original), (False, []))
                has_anchor_loss, warnings = content_loss(original, original.replace('ri:space-key="A"', 'ri:space-key="B"'))
                self.assertFalse(has_anchor_loss)
                self.assertTrue(any(warning in value for value in warnings))

    def test_nested_macro_change_reports_changed_parent_body_and_removed_child(self) -> None:
        before = (
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="title">Heading</ac:parameter>'
            '<ac:rich-text-body><ac:structured-macro ac:name="jira"></ac:structured-macro></ac:rich-text-body>'
            '</ac:structured-macro>'
        )
        replacement = (
            '<ac:structured-macro ac:name="panel">'
            '<ac:parameter ac:name="title">Heading</ac:parameter>'
            "<ac:rich-text-body>Updated text</ac:rich-text-body>"
            '</ac:structured-macro>'
        )

        self.assertEqual(
            content_loss(before, replacement),
            (False, ["1 panel macro(s) changed.", "1 jira macro(s) would be removed."]),
        )


class FileStorageChecks(unittest.TestCase):
    def test_unedited_storage_round_trips_byte_for_byte(self) -> None:
        storage = (Path(__file__).parent / "fixtures" / "page_text" / "runbook.storage.xml").read_text(encoding="utf-8")
        storage = re.sub(r'<ac:structured-macro ac:name="html".*?</ac:structured-macro>', "", storage, flags=re.S)
        storage += '\n<p>a&nbsp;b &amp; c<br /></p><ac:emoticon ac:name="smile" /><pre>preformatted</pre>'

        self.assertEqual(file_to_storage(storage, storage), storage)

    def test_only_new_pre_and_details_blocks_are_converted(self) -> None:
        body = (
            "<pre>kept</pre>\n<p>x&nbsp;y</p>"
            '<pre><code class="language-python">print(1 &lt; 2)</code></pre>'
            "<details><summary>More</summary><p>inner</p></details>"
        )

        result = file_to_storage(body, "<p>old</p><pre>kept</pre>")

        self.assertTrue(result.startswith("<pre>kept</pre>\n<p>x&nbsp;y</p>"))
        self.assertIn(
            '<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body><![CDATA[print(1 < 2)]]></ac:plain-text-body></ac:structured-macro>",
            result,
        )
        self.assertTrue(result.endswith('<ac:rich-text-body><p>inner</p></ac:rich-text-body></ac:structured-macro>'))

    def test_markup_inside_cdata_and_unparseable_markup_are_sent_unchanged(self) -> None:
        cdata = (
            '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
            "<![CDATA[<pre>x</pre><details>y</details>]]></ac:plain-text-body></ac:structured-macro>"
        )
        self.assertEqual(file_to_storage(cdata, None), cdata)
        self.assertEqual(file_to_storage("<p>a<br>b", None), "<p>a<br>b")

    def test_file_bodies_get_the_same_active_content_refusals(self) -> None:
        for body in ("<script>x</script>", '<ac:structured-macro ac:name="html"></ac:structured-macro>'):
            with self.subTest(body=body), self.assertRaises(InvalidRequest):
                file_to_storage(body, body)


if __name__ == "__main__":
    unittest.main()
