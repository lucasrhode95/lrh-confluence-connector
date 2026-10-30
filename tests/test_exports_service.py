"""Offline export layout and partial-failure checks using synthetic pages and files."""

from __future__ import annotations

import tempfile
import os
import stat
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from confluence_connector.domain.errors import InvalidRequest, NotFound, Upstream
from confluence_connector.domain.models import (
    PageAttachment,
    PageComment,
    PageParseScope,
    PageReadRecord,
    PageTextFormat,
    PersonSummary,
    ResolvedPageReference,
)
from confluence_connector.service.attachments import AttachmentDownloads
from confluence_connector.service.exports import MAX_EXPORT_PAGES, PageExports
from confluence_connector.service.storage_files import split_export_header


class PageExportChecks(unittest.IsolatedAsyncioTestCase):
    async def test_one_text_page_is_written_alone_with_exact_text_and_required_header(
        self,
    ) -> None:
        body = "# Sample\n\nBreadcrumb: Space › Sample\n\nParagraph\n"
        pages = _Pages({"55": _record("55", "Sample", body)})
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages("55", parse_scope={"header": False})
            self.assertTrue(result.filename.endswith("Sample (55) v2.txt"))
            self.assertRegex(result.filename, r"^\d{8}T\d{6}Z - ")
            self.assertLessEqual(len(result.filename), 160)
            self.assertEqual(Path(result.path).read_bytes(), body.encode("utf-8"))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(Path(result.path).stat().st_mode), 0o600)
            self.assertEqual(result.content_type, "text/plain; charset=utf-8")
            self.assertEqual((result.pages_exported, result.pages_failed), (1, 0))
        self.assertTrue(pages.scopes[0].header)

    async def test_multiple_pages_and_attachment_bytes_use_flat_zip_layout(
        self,
    ) -> None:
        attachments = [PageAttachment(attachment_id="att-1", filename="guide.txt")]
        pages = _Pages(
            {
                "55": _record("55", "First", "First text", attachments=attachments),
                "56": _record("56", "Second", "Second text"),
            }
        )
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages(
                ["55", "56"],
                include_attachments=True,
                parse_scope={"attachments": False},
            )
            with zipfile.ZipFile(result.path) as archive:
                names = archive.namelist()
                self.assertEqual(
                    set(names),
                    {
                        "First (55) v2.txt",
                        "First (55) v2/guide.txt",
                        "Second (56) v2.txt",
                    },
                )
                self.assertEqual(
                    archive.read("First (55) v2/guide.txt"), b"synthetic attachment"
                )
                self.assertEqual(archive.read("First (55) v2.txt"), b"First text")
            self.assertEqual(
                (result.pages_exported, result.attachments_exported), (2, 1)
            )
            self.assertEqual((result.pages_failed, result.attachments_failed), (0, 0))

    async def test_export_reuses_its_attachment_list_for_downloads(self) -> None:
        attachments = [PageAttachment(attachment_id="att-1", filename="guide.txt")]
        pages = _Pages(
            {
                "55": _record(
                    "55", "Synthetic export", "Export body", attachments=attachments
                )
            }
        )
        service, api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages("55", include_attachments=True)
            with zipfile.ZipFile(result.path) as archive:
                self.assertEqual(
                    archive.read("Synthetic export (55) v2/guide.txt"),
                    b"synthetic attachment",
                )
            self.assertEqual(api.list_calls, 1)
            self.assertEqual(api.download_calls, ["att-1"])

    async def test_rich_export_writes_body_and_comments_as_separate_files(
        self,
    ) -> None:
        storage_body = '<ac:structured-macro ac:name="panel"><p>A &amp; B</p></ac:structured-macro>'
        comment = PageComment(
            comment_id="c-1",
            author=PersonSummary(display_name="Synthetic Author"),
            body="<script>not markup</script> & text",
            selected_text="<selection>",
        )
        pages = _Pages(
            {
                "55": _record(
                    "55",
                    "Rich page",
                    storage_body,
                    page_format="rich",
                    comments=[comment],
                )
            }
        )
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages("55", format="rich")
            body_file = next(file for file in result.files if file.role == "body")
            comments_file = next(file for file in result.files if file.role == "comments")
            content = Path(body_file.location).read_text(encoding="utf-8")
            comments = Path(comments_file.location).read_text(encoding="utf-8")
            self.assertTrue(
                content.startswith(
                    "<!--\n# Rich page\nBreadcrumb: Space › Rich page\nVersion: 2\nPage ID: 55\n-->\n"
                )
            )
            self.assertIn(storage_body, content)
            self.assertNotIn("confluence-comments", content)
            self.assertTrue(comments.startswith('<section class="confluence-comments">'))
            self.assertIn("&lt;script&gt;not markup&lt;/script&gt; &amp; text", comments)
            self.assertIn("&lt;selection&gt;", comments)
            self.assertNotIn("<script>not markup</script>", comments)
            self.assertTrue(comments_file.location.endswith(".comments.html"))

    async def test_rich_export_without_comments_reads_back_as_page_identity_and_exact_body(self) -> None:
        storage_body = '<p>a&nbsp;b</p>\n<ac:structured-macro ac:name="toc" ac:macro-id="m-1" />'
        pages = _Pages({"55": _record("55", "Rich -- page", storage_body, page_format="rich")})
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages(
                "55", format="rich", parse_scope={"comments": False}
            )
            body_file = next(file for file in result.files if file.role == "body")
            header, body = split_export_header(Path(body_file.location).read_text(encoding="utf-8"))
        self.assertEqual((header.page_id, header.version), ("55", 2))
        self.assertEqual(body, storage_body)
        self.assertEqual([file.role for file in result.files], ["body"])

    async def test_rich_comments_file_is_rejected_as_a_body_file(self) -> None:
        comment = PageComment(comment_id="c-1", body="Synthetic comment")
        pages = _Pages({"55": _record("55", "Rich", "<p>Body</p>", page_format="rich", comments=[comment])})
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages("55", format="rich")
            comments_file = next(file for file in result.files if file.role == "comments")
            with self.assertRaisesRegex(InvalidRequest, "comments section"):
                split_export_header(Path(comments_file.location).read_text(encoding="utf-8"))

    async def test_rich_export_of_multiple_pages_stays_as_direct_files(self) -> None:
        pages = _Pages({
            "55": _record("55", "First", "<p>First</p>", page_format="rich"),
            "56": _record("56", "Second", "<p>Second</p>", page_format="rich"),
        })
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages(["55", "56"], format="rich", parse_scope={"comments": False})
        self.assertEqual([file.role for file in result.files], ["body", "body"])
        self.assertTrue(all(Path(file.location).suffix == ".html" for file in result.files))

    async def test_bulk_rich_export_is_one_archive_with_archive_role(self) -> None:
        pages = _Pages({"55": _record("55", "Rich", "<p>Body</p>", page_format="rich")})
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages(["55"] * 21, format="rich")
            with zipfile.ZipFile(result.path) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), 42)
                self.assertEqual(len({name.casefold() for name in names}), 42)
                self.assertEqual(sum(name.endswith(".comments.html") for name in names), 21)
        self.assertEqual([file.role for file in result.files], ["archive"])

    async def test_direct_rich_failure_has_a_separate_report_file(self) -> None:
        pages = _Pages({"55": _record("55", "Rich", "<p>Body</p>", page_format="rich")})
        service, _api, links = _service(pages)
        links.failures["missing"] = NotFound("Synthetic missing page")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages(["55", "missing"], format="rich")
            report = next(file for file in result.files if file.role == "report")
            report_text = Path(report.location).read_text(encoding="utf-8")
        self.assertIn("page: missing", report_text)
        self.assertEqual([file.role for file in result.files], ["body", "comments", "report"])

    async def test_failed_page_and_attachment_are_reported_in_archive(self) -> None:
        attachments = [PageAttachment(attachment_id="att-fail", filename="broken.bin")]
        pages = _Pages(
            {"55": _record("55", "First", "First body", attachments=attachments)}
        )
        service, _api, links = _service(pages, failed_downloads={"att-fail"})
        links.failures["missing"] = NotFound("Synthetic missing page")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages(
                ["55", "missing"], include_attachments=True
            )
            with zipfile.ZipFile(result.path) as archive:
                report = archive.read("_export-report.txt").decode("utf-8")
                self.assertIn("page: missing", report)
                self.assertIn("attachment: Page 55 (First) — broken.bin", report)
                self.assertIn("Synthetic missing page", report)
                self.assertIn("Upstream", report)
                self.assertIn("First (55) v2.txt", archive.namelist())
                self.assertNotIn("First (55) v2/broken.bin", archive.namelist())
            self.assertEqual((result.pages_exported, result.pages_failed), (1, 1))
            self.assertEqual(result.attachments_failed, 1)

    async def test_page_limit_is_rejected_before_resolver_or_api_calls(self) -> None:
        service, api, links = _service(_Pages({}))
        self.assertEqual(MAX_EXPORT_PAGES, 500)
        with self.assertRaisesRegex(InvalidRequest, "1 to 500"):
            await service.export_pages(["55"] * 501)
        self.assertEqual(links.calls, 0)
        self.assertEqual(api.list_calls, 0)

    async def test_duplicate_page_names_get_distinct_safe_archive_members(self) -> None:
        record = _record("55", "CON: quarterly/plan. " * 30, "Page text")
        service, _api, _links = _service(_Pages({"55": record}))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages(["55", "55"])
            with zipfile.ZipFile(result.path) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), 2)
                self.assertEqual(len({name.casefold() for name in names}), 2)
                self.assertTrue(all(len(name) <= 180 for name in names))
                self.assertTrue(
                    all("/" not in name and ":" not in name for name in names)
                )
                self.assertTrue(all(name.endswith(" (55) v2.txt") for name in names))
                self.assertTrue(all(len(name.encode("utf-8")) <= 240 for name in names))

    async def test_rich_multibyte_title_keeps_body_and_comments_within_filename_limits(self) -> None:
        pages = _Pages({"55": _record(
            "55", "😀" * 130, "<p>Synthetic page</p>", page_format="rich",
            comments=[PageComment(comment_id="c-1", body="Synthetic comment")],
        )})
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages("55", format="rich")
            self.assertEqual([file.role for file in result.files], ["body", "comments"])
            for file in result.files:
                filename = Path(file.location).name
                self.assertLessEqual(len(filename.encode("utf-8")), 240)
                self.assertLessEqual(len(filename.encode("utf-16-le")) // 2, 240)
                self.assertTrue(Path(file.location).is_file())

    async def test_multibyte_title_is_shortened_to_a_safe_filesystem_component(self) -> None:
        pages = _Pages({"55": _record("55", "😀" * 130, "Synthetic page")})
        service, _api, _links = _service(pages)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("confluence_connector.config.data_dir", return_value=Path(directory)),
        ):
            result = await service.export_pages("55")
            filename = Path(result.path).name
            self.assertLessEqual(len(filename.encode("utf-8")), 240)
            self.assertLessEqual(len(filename.encode("utf-16-le")) // 2, 240)


def _service(
    pages: _Pages,
    *,
    failed_downloads: set[str] | None = None,
) -> tuple[PageExports, _Api, _Links]:
    api = _Api(
        failed_downloads=failed_downloads or set(),
        attachments={
            page_id: record.attachments for page_id, record in pages.records.items()
        },
    )
    links = _Links()
    attachments = AttachmentDownloads(api, links, "sample-user")  # type: ignore[arg-type]
    service = PageExports(api, links, pages, attachments, "sample-user")  # type: ignore[arg-type]
    return service, api, links


def _record(
    page_id: str,
    title: str,
    text: str,
    *,
    page_format: PageTextFormat = "text",
    attachments: list[PageAttachment] | None = None,
    comments: list[PageComment] | None = None,
) -> PageReadRecord:
    return PageReadRecord(
        page_id=page_id,
        title=title,
        version=2,
        extracted_at="2026-10-19T00:00:00Z",
        format=page_format,
        text=text,
        header=f"# {title}\nBreadcrumb: Space › {title}\nVersion: 2",
        attachments=attachments or [],
        comments=comments or [],
    )


class _Pages:
    def __init__(self, records: dict[str, PageReadRecord]) -> None:
        self.records = records
        self.scopes: list[PageParseScope] = []

    async def read_full_page(
        self,
        page_id: str,
        *,
        version: int | None,
        page_format: PageTextFormat,
        parse_scope: PageParseScope,
        attachment_override: list[PageAttachment] | None = None,
    ) -> PageReadRecord:
        self.scopes.append(parse_scope)
        if page_id not in self.records:
            raise NotFound("Synthetic missing page")
        record = self.records[page_id].model_copy(deep=True)
        record.format = page_format
        if attachment_override is not None:
            record.attachments = attachment_override
        return record


class _Links:
    def __init__(self) -> None:
        self.calls = 0
        self.failures: dict[str, Exception] = {}

    async def resolve(
        self, reference: str, *, expected_username: str
    ) -> ResolvedPageReference:
        self.calls += 1
        if reference in self.failures:
            raise self.failures[reference]
        return ResolvedPageReference(page_id=reference)


class _Api:
    def __init__(
        self,
        *,
        failed_downloads: set[str],
        attachments: dict[str, list[PageAttachment]],
    ) -> None:
        self.failed_downloads = failed_downloads
        self.attachments = attachments
        self.list_calls = 0
        self.download_calls: list[str] = []

    async def list_page_attachments(
        self, page_id: str, *, expected_username: str
    ) -> list[PageAttachment]:
        self.list_calls += 1
        return self.attachments.get(page_id, [])

    async def download_attachment(
        self,
        attachment: PageAttachment,
        destination,
        *,
        expected_username: str,
    ) -> tuple[str | None, int]:
        self.download_calls.append(attachment.attachment_id)
        if attachment.attachment_id in self.failed_downloads:
            destination.write(b"partial")
            raise Upstream("Synthetic attachment failure")
        content = b"synthetic attachment"
        destination.write(content)
        return attachment.media_type, len(content)


if __name__ == "__main__":
    unittest.main()
