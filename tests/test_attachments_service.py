"""Offline checks for selected attachment downloads and collision-safe local paths."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from confluence_connector.domain.errors import InvalidRequest, Upstream
from confluence_connector.domain.models import PageAttachment, ResolvedPageReference
from confluence_connector.service.attachments import AttachmentDownloads
from confluence_connector.service import localfiles


class AttachmentDownloadChecks(unittest.IsolatedAsyncioTestCase):
    async def test_batch_prepares_and_cleans_the_output_directory_once(self) -> None:
        api = _AttachmentApi([PageAttachment(attachment_id=str(i), filename=f"file-{i}.txt") for i in range(3)])
        service = _service(api)
        with tempfile.TemporaryDirectory() as directory, patch.object(service, "_download_directory", return_value=Path(directory)) as prepare:
            result = await service.download_attachments("55", ["0", "1", "2"])
        self.assertEqual(len(result.files), 3)
        prepare.assert_called_once()

    async def test_output_directory_failure_is_reported_for_each_selected_attachment(self) -> None:
        service = _service(_AttachmentApi([PageAttachment(attachment_id="1", filename="one.txt")]))
        with patch.object(service, "_download_directory", side_effect=PermissionError("synthetic")):
            result = await service.download_attachments("55", ["1"])
        self.assertFalse(result.files)
        self.assertEqual(result.failures[0].attachment_id, "1")
        self.assertIn("PermissionError", result.failures[0].error)

    async def test_selected_attachment_is_saved_with_safe_path_and_source_metadata(self) -> None:
        api = _AttachmentApi([PageAttachment(attachment_id="att-1", filename="NUL.txt", media_type="text/plain")])
        service = _service(api)
        with tempfile.TemporaryDirectory() as directory, patch(
            "confluence_connector.config.data_dir", return_value=Path(directory)
        ):
            result = await service.download_attachments("55", ["att-1"])
            self.assertEqual(len(result.files), 1)
            saved = result.files[0]
            self.assertEqual(saved.filename, "_NUL.txt")
            self.assertEqual(Path(saved.path).read_bytes(), b"synthetic attachment")
            self.assertEqual(saved.size_bytes, len(b"synthetic attachment"))
            self.assertEqual(saved.media_type, "text/plain")
            self.assertTrue(Path(saved.path).parent.parent.name == "downloads")
            self.assertEqual(result.failures, [])
        self.assertEqual(api.downloaded_ids, ["att-1"])

    async def test_unknown_attachment_id_is_reported_without_a_download(self) -> None:
        api = _AttachmentApi([PageAttachment(attachment_id="att-1", filename="report.txt")])
        result = await _service(api).download_attachments("55", ["missing"])
        self.assertEqual(result.files, [])
        self.assertEqual(result.failures[0].attachment_id, "missing")
        self.assertEqual(api.downloaded_ids, [])

    async def test_multibyte_attachment_filename_fits_common_filesystem_limits(self) -> None:
        api = _AttachmentApi(
            [PageAttachment(attachment_id="att-1", filename=f"{'😀' * 130}.txt")]
        )
        service = _service(api)
        with tempfile.TemporaryDirectory() as directory, patch(
            "confluence_connector.config.data_dir", return_value=Path(directory)
        ):
            result = await service.download_attachments("55", ["att-1"])
            filename = result.files[0].filename
            self.assertLessEqual(len(filename.encode("utf-8")), 240)
            self.assertLessEqual(len(filename.encode("utf-16-le")) // 2, 240)

    async def test_failed_download_removes_its_partial_file(self) -> None:
        api = _AttachmentApi(
            [PageAttachment(attachment_id="att-1", filename="report.txt")],
            failure=Upstream("Synthetic download failure"),
        )
        service = _service(api)
        with tempfile.TemporaryDirectory() as directory, patch(
            "confluence_connector.config.data_dir", return_value=Path(directory)
        ):
            result = await service.download_attachments("55", ["att-1"])
            self.assertEqual(result.files, [])
            self.assertIn("Upstream", result.failures[0].error)
            self.assertEqual(list(Path(directory).rglob("report.txt")), [])

    async def test_duplicate_ids_are_rejected_before_page_or_attachment_requests(self) -> None:
        api = _AttachmentApi([])
        with self.assertRaises(InvalidRequest):
            await _service(api).download_attachments("55", ["att-1", "att-1"])
        self.assertEqual(api.list_calls, 0)

    def test_local_claims_use_distinct_names_for_concurrent_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = localfiles.claim(folder, "same.txt")
            second = localfiles.claim(folder, "same.txt")
            self.assertEqual((first.name, second.name), ("same.txt", "same (2).txt"))


def _service(api: _AttachmentApi) -> AttachmentDownloads:
    return AttachmentDownloads(api, _Links(), "sample-user")  # type: ignore[arg-type]


class _Links:
    async def resolve(self, reference: str, *, expected_username: str) -> ResolvedPageReference:
        return ResolvedPageReference(page_id=reference, version=None)


class _AttachmentApi:
    def __init__(self, attachments: list[PageAttachment], *, failure: Exception | None = None) -> None:
        self.attachments = attachments
        self.failure = failure
        self.list_calls = 0
        self.downloaded_ids: list[str] = []

    async def list_page_attachments(self, page_id: str, *, expected_username: str) -> list[PageAttachment]:
        self.list_calls += 1
        return self.attachments

    async def download_attachment(
        self,
        attachment: PageAttachment,
        destination,
        *,
        expected_username: str,
    ) -> tuple[str | None, int]:
        self.downloaded_ids.append(attachment.attachment_id)
        destination.write(b"partial")
        if self.failure is not None:
            raise self.failure
        destination.seek(0)
        destination.truncate()
        destination.write(b"synthetic attachment")
        return attachment.media_type, len(b"synthetic attachment")


if __name__ == "__main__":
    unittest.main()
