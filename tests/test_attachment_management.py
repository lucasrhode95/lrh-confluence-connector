"""Offline checks for attachment ownership, preflight and safe mutation outcomes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from confluence_connector.domain.errors import InvalidRequest, UncertainWrite
from confluence_connector.domain.models import PageAttachment, RemotePageContent, PageMetadata, ResolvedPageReference
from confluence_connector.service.attachment_management import AttachmentManagement


class AttachmentManagementChecks(unittest.IsolatedAsyncioTestCase):
    async def test_upload_adds_a_version_and_reports_mime_type(self) -> None:
        api = _Api()
        api.upload_page_attachment.return_value = PageAttachment(
            attachment_id="91", filename="diagram.svg", version=2,
            media_type="image/svg+xml", download_url="https://wiki.example.test/download/91",
        )
        service = _service(api)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagram.svg"
            path.write_bytes(b"<svg />")
            result = await service.upload_attachments("55", [str(path)], comment="second version")
        self.assertEqual(result.attachments[0].version, 2)
        self.assertEqual(result.attachments[0].media_type, "image/svg+xml")
        self.assertEqual(api.upload_page_attachment.await_args.kwargs["content"], b"<svg />")
        self.assertEqual(api.upload_page_attachment.await_args.kwargs["comment"], "second version")

    async def test_upload_existing_filename_uses_attachment_version_route(self) -> None:
        api = _Api(attachments=[PageAttachment(attachment_id="91", filename="diagram.svg", version=1)])
        api.upload_attachment_version.return_value = PageAttachment(
            attachment_id="91", filename="diagram.svg", version=2, media_type="image/svg+xml"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "diagram.svg"
            path.write_bytes(b"<svg />")
            result = await _service(api).upload_attachments("55", [str(path)])
        self.assertEqual(result.attachments[0].version, 2)
        api.upload_page_attachment.assert_not_awaited()
        api.upload_attachment_version.assert_awaited_once()
        self.assertEqual(api.upload_attachment_version.await_args.args[:2], ("55", "91"))

    async def test_upload_preflights_duplicate_names_before_remote_write(self) -> None:
        api = _Api()
        service = _service(api)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "same.txt"
            second = Path(directory) / "SAME.TXT"
            first.write_text("one")
            second.write_text("two")
            with self.assertRaisesRegex(InvalidRequest, "duplicate filenames"):
                await service.upload_attachments("55", [str(first), str(second)])
        api.page_supports_update.assert_not_awaited()
        api.upload_page_attachment.assert_not_awaited()

    async def test_update_requires_attachment_ownership_and_versions_metadata(self) -> None:
        api = _Api(attachments=[PageAttachment(attachment_id="91", filename="old.png", version=3, media_type="image/png")])
        api.get_page_attachment.return_value = PageAttachment(
            attachment_id="91", filename="old.png", version=3, media_type="image/png", container_page_id="55"
        )
        api.update_page_attachment.return_value = PageAttachment(attachment_id="91", filename="new.svg", version=4, media_type="image/svg+xml")
        result = await _service(api).update_attachment("55", "91", filename="new.svg", media_type="image/svg+xml", comment="rename")
        self.assertEqual((result.attachments[0].filename, result.attachments[0].version), ("new.svg", 4))
        self.assertEqual(api.update_page_attachment.await_args.args[1].version, 3)
        api.get_page_attachment.assert_awaited_once_with("91", status="current", expected_username="sample-user")
        with self.assertRaisesRegex(InvalidRequest, "does not belong"):
            await _service(api).update_attachment("55", "92", filename="missing.txt")

    async def test_trash_refuses_attachment_still_used_in_page_body(self) -> None:
        attachment = PageAttachment(attachment_id="91", filename="diagram.svg", version=1)
        api = _Api(attachments=[attachment], storage='<ac:image><ri:attachment ri:filename="diagram.svg" /></ac:image>')
        with self.assertRaisesRegex(InvalidRequest, "still references"):
            await _service(api).trash_attachments("55", ["91"])
        api.trash_page_attachment.assert_not_awaited()

    async def test_trash_can_be_confirmed_and_reports_delete_permission(self) -> None:
        attachment = PageAttachment(attachment_id="91", filename="loose.txt", version=1)
        api = _Api(attachments=[attachment])
        result = await _service(api).trash_attachments("55", ["91"], allow_referenced=True)
        self.assertEqual(result.attachments[0].attachment_id, "91")
        self.assertIn("other pages are not searched", result.notes[0])
        api.attachment_supports_delete.assert_awaited_once_with("91", expected_username="sample-user")
        api.trash_page_attachment.assert_awaited_once()

    async def test_trash_permission_denial_is_reported_separately(self) -> None:
        api = _Api(attachments=[PageAttachment(attachment_id="91", filename="loose.txt")])
        api.attachment_supports_delete.return_value = False
        result = await _service(api).trash_attachments("55", ["91"])
        self.assertEqual(result.failures[0].error.split(":", 1)[0], "PermissionDenied")
        self.assertIn("attachment delete permission", result.failures[0].error)
        api.trash_page_attachment.assert_not_awaited()

    async def test_restore_checks_original_page_ownership(self) -> None:
        api = _Api()
        api.get_page_attachment.return_value = PageAttachment(
            attachment_id="91", filename="loose.txt", version=2, container_page_id="56"
        )
        result = await _service(api).restore_attachments("55", ["91"])
        self.assertIn("does not belong", result.failures[0].error)
        api.restore_page_attachment.assert_not_awaited()

    async def test_uncertain_trash_stops_and_marks_remaining_ids_not_attempted(self) -> None:
        api = _Api(attachments=[
            PageAttachment(attachment_id="91", filename="one.txt"),
            PageAttachment(attachment_id="92", filename="two.txt"),
        ])
        api.trash_page_attachment.side_effect = UncertainWrite("inspect")
        result = await _service(api).trash_attachments("55", ["91", "92"])
        self.assertEqual(len(result.failures), 2)
        self.assertIn("UncertainWrite", result.failures[0].error)
        self.assertIn("Not attempted", result.failures[1].error)
        self.assertEqual(api.trash_page_attachment.await_count, 1)


def _service(api: _Api) -> AttachmentManagement:
    return AttachmentManagement(api, _Links(), "sample-user")  # type: ignore[arg-type]


class _Links:
    async def resolve(self, reference: str, *, expected_username: str) -> ResolvedPageReference:
        return ResolvedPageReference(page_id=reference)


class _Api:
    def __init__(self, *, attachments: list[PageAttachment] | None = None, storage: str = "") -> None:
        self.attachments = attachments or []
        self.page_supports_update = AsyncMock(return_value=True)
        self.list_page_attachments = AsyncMock(return_value=self.attachments)
        self.upload_page_attachment = AsyncMock()
        self.upload_attachment_version = AsyncMock()
        self.update_page_attachment = AsyncMock()
        self.attachment_supports_delete = AsyncMock(return_value=True)
        self.trash_page_attachment = AsyncMock()
        self.get_page_attachment = AsyncMock()
        self.restore_page_attachment = AsyncMock()
        self.get_page_content = AsyncMock(return_value=RemotePageContent(
            metadata=PageMetadata(page_id="55", title="Synthetic", content_type="page"),
            view_body="", storage_body=storage, version=1,
        ))
