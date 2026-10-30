"""Offline checks for rich export headers and `body_file` reading."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from confluence_connector.domain.errors import InvalidRequest
from confluence_connector.service.storage_files import (
    read_storage_file,
    rich_export_content,
    split_export_header,
)


class StorageFileChecks(unittest.TestCase):
    def test_written_header_reads_back_with_its_page_and_version(self) -> None:
        content = rich_export_content("# Title -- with dashes\nVersion: 4 of 6", "55", "<p>body</p>")

        header, body = split_export_header(content)

        self.assertNotIn("--", content.split("-->")[0][4:])
        self.assertEqual((header.page_id, header.version), ("55", 4))
        self.assertEqual(body, "<p>body</p>")

    def test_windows_line_endings_in_the_header_are_accepted_and_the_body_is_kept(self) -> None:
        header, body = split_export_header("<!--\r\n# T\r\nVersion: 3\r\nPage ID: 9\r\n-->\r\n<p>x</p>\r\n")

        self.assertEqual((header.page_id, header.version), ("9", 3))
        self.assertEqual(body, "<p>x</p>\r\n")

    def test_plain_storage_has_no_header(self) -> None:
        header, body = split_export_header("<p>plain</p><!-- not a header -->")

        self.assertIsNone(header)
        self.assertEqual(body, "<p>plain</p><!-- not a header -->")

    def test_header_without_one_page_id_and_version_is_refused(self) -> None:
        for header_text in ("# T\nVersion: 3", "# T\nPage ID: 9", "# T\nPage ID: 9\nPage ID: 10\nVersion: 3"):
            with self.subTest(header_text=header_text), self.assertRaisesRegex(InvalidRequest, "export the page again"):
                split_export_header(f"<!--\n{header_text}\n-->\n<p>x</p>")

    def test_title_line_cannot_supply_the_page_identity(self) -> None:
        with self.assertRaisesRegex(InvalidRequest, "export the page again"):
            split_export_header("<!--\n# Page ID: 9\n# Version: 3\n-->\n<p>x</p>")

    def test_exported_comments_section_is_refused_with_or_without_a_header(self) -> None:
        section = '<section class="confluence-comments"><h2>Comments</h2></section>'
        for text in (rich_export_content("# T\nVersion: 3", "9", f"<p>x</p>{section}"), f"<p>x</p>{section}"):
            with self.subTest(text=text[:20]), self.assertRaisesRegex(InvalidRequest, "comments section"):
                split_export_header(text)

    def test_read_requires_an_absolute_utf8_file_and_drops_a_byte_order_mark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory, "page.html")
            good.write_bytes("﻿<p>café</p>".encode("utf-8"))
            self.assertEqual(read_storage_file(str(good)), "<p>café</p>")

            latin = Path(directory, "latin.html")
            latin.write_bytes("<p>café</p>".encode("latin-1"))
            with self.assertRaisesRegex(InvalidRequest, "UTF-8"):
                read_storage_file(str(latin))
            with self.assertRaisesRegex(InvalidRequest, "readable file"):
                read_storage_file(str(Path(directory, "missing.html")))
            with self.assertRaisesRegex(InvalidRequest, "readable file"):
                read_storage_file(directory)
        with self.assertRaisesRegex(InvalidRequest, "absolute"):
            read_storage_file("relative/page.html")
        with self.assertRaisesRegex(InvalidRequest, "absolute"):
            read_storage_file(None)


if __name__ == "__main__":
    unittest.main()
