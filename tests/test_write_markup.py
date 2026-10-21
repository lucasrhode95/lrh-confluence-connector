"""Offline checks for D5 rich-input conversion and active-content refusal."""

from __future__ import annotations

import unittest

from confluence_connector.domain.errors import InvalidRequest
from confluence_connector.service.write_markup import content_loss, to_storage


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

    def test_nested_macro_change_does_not_report_its_unchanged_parent(self) -> None:
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

        self.assertEqual(content_loss(before, replacement), (False, ["1 jira macro(s) would be removed."]))


if __name__ == "__main__":
    unittest.main()
