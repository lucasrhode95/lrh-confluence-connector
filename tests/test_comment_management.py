"""Synthetic checks for comment ownership, versioning and destructive confirmation."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from confluence_connector.domain.errors import Conflict, InvalidRequest, Upstream
from confluence_connector.domain.models import PageMetadata, RemotePageComment, ResolvedPageReference
from confluence_connector.service.comment_management import CommentManagement


class CommentManagementChecks(unittest.IsolatedAsyncioTestCase):
    async def test_add_comment_and_reply_validate_page_ownership(self) -> None:
        api = _Api()
        result = await _service(api).add_comment("55", "synthetic note", format="text")

        self.assertEqual(result.comment.comment_id, "501")
        self.assertEqual(result.comment.page_id, "55")
        self.assertEqual(result.comment.version, 1)
        self.assertEqual(result.comment.body, "synthetic note")
        self.assertEqual(result.warnings, [])
        self.assertFalse(result.notification_suppression_supported)
        self.assertIsNone(result.notification_note)
        self.assertEqual(
            api.create_comment.await_args.args[:2], ("55", "<p>synthetic note</p>")
        )

        api.comments["500"] = _remote_comment(container_page_id="56")
        with self.assertRaisesRegex(InvalidRequest, "parent comment must belong"):
            await _service(api).add_comment("55", "reply", parent_comment_id="500")
        api.create_comment.assert_awaited_once()

    async def test_saved_comment_that_cannot_be_reread_keeps_known_facts(self) -> None:
        api = _Api()
        api.reread_error = Upstream("synthetic reread failure")

        result = await _service(api).add_comment("55", "synthetic note", format="text")

        self.assertEqual((result.comment.comment_id, result.comment.version, result.comment.body), ("501", 1, ""))
        self.assertIn("could not be read back (Upstream)", result.warnings[0])

    async def test_get_comment_returns_identity_and_inline_metadata_without_page_reads(self) -> None:
        api = _Api()
        api.comments["500"] = _remote_comment(
            location="inline", resolution="resolved", selected_text="selected text"
        )

        result = await _service(api).get_comment("500")

        self.assertEqual((result.comment_id, result.version, result.page_id), ("500", 3, "55"))
        self.assertEqual(result.body, "A synthetic comment")
        self.assertEqual((result.location, result.resolution, result.selected_text), ("inline", "resolved", "selected text"))
        self.assertEqual(result.page_url, "https://wiki.example.test/spaces/SYNTH/pages/55/Synthetic")
        api.get_page_metadata.assert_not_awaited()

    async def test_update_refuses_stale_version_then_returns_the_reread_comment(self) -> None:
        api = _Api()
        service = _service(api)
        with self.assertRaisesRegex(Conflict, "expected_version was 2"):
            await service.update_comment("500", "replacement", expected_version=2)
        api.update_comment.assert_not_awaited()

        result = await service.update_comment(
            "500", "replacement", expected_version=3, format="text", page="55",
            notify_watchers=False,
        )
        self.assertEqual(result.comment.version, 4)
        self.assertEqual(result.comment.body, "replacement")
        self.assertTrue(result.notification_suppression_supported)
        self.assertEqual(api.update_comment.await_args.args[:3], ("500", "55", "<p>replacement</p>"))
        self.assertEqual(api.update_comment.await_args.kwargs["next_version"], 4)
        self.assertFalse(api.update_comment.await_args.kwargs["notify_watchers"])
        api.get_page_metadata.assert_not_awaited()

    async def test_update_and_delete_verify_optional_page_and_delete_confirmation(self) -> None:
        api = _Api()
        service = _service(api)
        with self.assertRaisesRegex(InvalidRequest, "does not belong"):
            await service.update_comment("500", "replacement", expected_version=3, page="56")
        api.update_comment.assert_not_awaited()

        with self.assertRaisesRegex(InvalidRequest, "confirm_permanent_delete=true"):
            await service.delete_comment("500", confirm_permanent_delete=False)
        api.delete_comment.assert_not_awaited()

        with self.assertRaisesRegex(InvalidRequest, "does not belong"):
            await service.delete_comment("500", confirm_permanent_delete=True, page="56")
        api.delete_comment.assert_not_awaited()

        result = await service.delete_comment(
            "500", confirm_permanent_delete=True, page="55", notify_watchers=False
        )
        self.assertEqual((result.comment_id, result.page_id), ("500", "55"))
        self.assertEqual(result.page_url, "https://wiki.example.test/spaces/SYNTH/pages/55/Synthetic")
        self.assertTrue(result.permanent)
        self.assertFalse(result.notification_suppression_supported)
        self.assertIsNotNone(result.notification_note)
        api.delete_comment.assert_awaited_once_with("500", expected_username="sample-user")


def _service(api: _Api) -> CommentManagement:
    return CommentManagement(api, _Links(), "sample-user", "https://wiki.example.test")  # type: ignore[arg-type]


def _remote_comment(
    *,
    comment_id: str = "500",
    version: int = 3,
    body: str = "<p>A synthetic comment</p>",
    container_page_id: str = "55",
    location: str | None = None,
    resolution: str | None = None,
    selected_text: str | None = None,
) -> RemotePageComment:
    return RemotePageComment(
        comment_id=comment_id,
        parent_comment_id="499",
        version=version,
        container_page_id=container_page_id,
        container_page_url=f"https://wiki.example.test/spaces/SYNTH/pages/{container_page_id}/Synthetic",
        body_view=body,
        body_storage=body,
        location=location,
        resolution=resolution,
        selected_text=selected_text,
    )


class _Api:
    """Stores comments so a write can be reread the way Confluence returns it."""

    def __init__(self) -> None:
        self.comments = {"500": _remote_comment()}
        self.reread_error: Exception | None = None
        self.get_page_metadata = AsyncMock(side_effect=_metadata)
        self.create_comment = AsyncMock(side_effect=self._create)
        self.update_comment = AsyncMock(side_effect=self._update)
        self.delete_comment = AsyncMock()

    async def get_comment(self, comment_id: str, *, expected_username: str) -> RemotePageComment:
        if comment_id != "500" and self.reread_error is not None:
            raise self.reread_error
        return self.comments[comment_id]

    async def _create(
        self, page_id: str, storage_body: str, *, expected_username: str, parent_comment_id: str | None = None
    ) -> str:
        self.comments["501"] = _remote_comment(
            comment_id="501", version=1, body=storage_body, container_page_id=page_id
        )
        return "501"

    async def _update(
        self, comment_id: str, page_id: str, storage_body: str, *, next_version: int, **kwargs: object
    ) -> None:
        self.comments[comment_id] = _remote_comment(
            comment_id=comment_id, version=next_version, body=storage_body, container_page_id=page_id
        )


class _Links:
    async def resolve(self, reference: str, *, expected_username: str) -> ResolvedPageReference:
        return ResolvedPageReference(page_id=reference)


def _metadata(page_id: str, *, expected_username: str) -> PageMetadata:
    return PageMetadata(
        page_id=page_id,
        title="Synthetic",
        content_type="page",
        status="current",
        url=f"https://wiki.example.test/spaces/SYNTH/pages/{page_id}/Synthetic",
    )
