"""Selected page attachment downloads into private, temporary local output folders."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from confluence_connector.domain.errors import ConnectorError, InvalidRequest
from confluence_connector.domain.models import (
    AttachmentDownloadFailure,
    AttachmentDownloadResult,
    PageAttachment,
    SavedAttachment,
)
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service import localfiles
from confluence_connector.domain.validation import page_references
from confluence_connector.service.errors import raise_account_error
from confluence_connector.service.filenames import (
    MAX_FILENAME_COMPONENT_UNITS,
    safe_filename_stem,
    sanitize_filename_component,
)

MAX_ATTACHMENT_IDS = 500
MAX_FILENAME_LENGTH = 180


class AttachmentDownloads:
    """Validate one page and selected attachment IDs, then save each file independently."""

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._download_limit = asyncio.Semaphore(6)

    async def download_attachments(
        self,
        page: str,
        attachment_ids: list[str],
    ) -> AttachmentDownloadResult:
        """Entry point: validate the page and requested IDs before listing or downloading files."""
        page_reference = page_references(page, maximum=1)[0]
        selected_ids = _attachment_ids(attachment_ids)
        resolved = await self._links.resolve(page_reference, expected_username=self._username)
        if not resolved.page_id.isdecimal():
            raise InvalidRequest("The selected reference did not resolve to a numeric page ID.")
        attachments = await self._api.list_page_attachments(resolved.page_id, expected_username=self._username)
        by_id = {attachment.attachment_id: attachment for attachment in attachments}
        missing = [
            AttachmentDownloadFailure(
                attachment_id=attachment_id,
                error=(
                    "The attachment was not in the readable attachments for this page."
                ),
            )
            for attachment_id in selected_ids
            if attachment_id not in by_id
        ]
        directory = self._download_directory()
        tasks = [
            self.download_to_directory(by_id[attachment_id], directory)
            for attachment_id in selected_ids
            if attachment_id in by_id
        ]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        files: list[SavedAttachment] = []
        failures = list(missing)
        for attachment_id, outcome in zip(
            (value for value in selected_ids if value in by_id), outcomes, strict=True
        ):
            if isinstance(outcome, SavedAttachment):
                files.append(outcome)
                continue
            if isinstance(outcome, ConnectorError):
                raise_account_error(outcome)
                failures.append(
                    AttachmentDownloadFailure(
                        attachment_id=attachment_id,
                        filename=by_id[attachment_id].filename,
                        error=f"{type(outcome).__name__}: {outcome}",
                    )
                )
                continue
            if isinstance(outcome, OSError):
                failures.append(
                    AttachmentDownloadFailure(
                        attachment_id=attachment_id,
                        filename=by_id[attachment_id].filename,
                        error=f"LocalFileError: {type(outcome).__name__}",
                    )
                )
                continue
            if isinstance(outcome, BaseException):
                raise outcome
            raise AssertionError("Attachment download returned an unexpected result.")
        return AttachmentDownloadResult(page_id=resolved.page_id, files=files, failures=failures)

    async def download_to_directory(self, attachment: PageAttachment, directory: Path) -> SavedAttachment:
        """Download one mapped attachment into a caller-owned temporary directory."""
        async with self._download_limit:
            return await self._download_to_directory(attachment, directory)

    async def _download_to_directory(self, attachment: PageAttachment, directory: Path) -> SavedAttachment:
        """Write a mapped attachment to a new sanitized name and remove partial files on failure."""
        filename = _safe_filename(attachment.filename)
        try:
            target = localfiles.claim(directory, filename)
        except FileNotFoundError:
            localfiles.private_dir(directory)
            target = localfiles.claim(directory, filename)
        try:
            with target.open("wb") as stream:
                content_type, size = await self._api.download_attachment(
                    attachment,
                    stream,
                    expected_username=self._username,
                )
            return SavedAttachment(
                attachment_id=attachment.attachment_id,
                filename=target.name,
                path=str(target),
                media_type=content_type or attachment.media_type,
                size_bytes=size,
            )
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def _download_directory(self) -> Path:
        """Use the shared downloads folder while keeping files separated after an account switch."""
        fingerprint = hashlib.sha256(self._username.casefold().encode("utf-8")).hexdigest()[:16]
        return localfiles.private_dir(localfiles.kept_dir("downloads") / fingerprint)


def _attachment_ids(value: object) -> list[str]:
    """Validate selected source IDs once at the public tool boundary."""
    if not isinstance(value, list) or not value or len(value) > MAX_ATTACHMENT_IDS:
        raise InvalidRequest(f"attachment_ids must contain 1 to {MAX_ATTACHMENT_IDS} IDs.")
    result: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 255
            or any(ord(character) < 32 for character in item)
        ):
            raise InvalidRequest("attachment_ids contains an invalid ID.")
        result.append(item.strip())
    if len(set(result)) != len(result):
        raise InvalidRequest("attachment_ids must not contain duplicates.")
    return result


def _safe_filename(value: str) -> str:
    """Replace path and Windows-invalid characters, device names and excessive lengths."""
    filename = sanitize_filename_component(value)
    original_suffix = Path(filename).suffix
    suffix = original_suffix[:20]
    stem = filename[: -len(original_suffix)] if original_suffix else filename
    safe_stem = safe_filename_stem(
        stem,
        suffix=suffix,
        maximum_length=MAX_FILENAME_LENGTH,
        maximum_component_units=MAX_FILENAME_COMPONENT_UNITS,
        fallback="attachment",
    )
    return f"{safe_stem}{suffix}"
