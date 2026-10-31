"""Create, version, update, trash and restore page attachments."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from bs4 import BeautifulSoup

from confluence_connector.domain.errors import ConnectorError, InvalidRequest, PermissionDenied, UncertainWrite
from confluence_connector.domain.models import (
    AttachmentChange,
    AttachmentChangeFailure,
    AttachmentMutationResult,
    PageAttachment,
    ResolvedPageReference,
)
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service.errors import raise_account_error
from confluence_connector.domain.validation import page_references

MAX_ATTACHMENT_MUTATIONS = 100


class AttachmentManagement:
    """Validate attachment ownership and preflight inputs before invoking remote writes."""

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str) -> None:
        self._api = api
        self._links = links
        self._username = username

    async def upload_attachments(
        self, page: str, file_paths: list[str], *, comment: str | None = None
    ) -> AttachmentMutationResult:
        paths = _upload_paths(file_paths)
        clean_comment = _comment(comment)
        resolved = await self._resolve_page(page)
        await self._require_page_update(resolved.page_id)
        prepared = [(path, path.name, _media_type(path.name), content) for path, content in paths]
        existing = {
            item.filename.casefold(): item
            for item in await self._api.list_page_attachments(
                resolved.page_id, expected_username=self._username
            )
        }
        result = AttachmentMutationResult(page_id=resolved.page_id)
        for index, (_path, filename, media_type, content) in enumerate(prepared):
            try:
                current = existing.get(filename.casefold())
                if current is None:
                    attachment = await self._api.upload_page_attachment(
                        resolved.page_id,
                        filename=filename,
                        content=content,
                        media_type=media_type,
                        comment=clean_comment,
                        expected_username=self._username,
                    )
                else:
                    attachment = await self._api.upload_attachment_version(
                        resolved.page_id,
                        current.attachment_id,
                        filename=filename,
                        content=content,
                        media_type=media_type,
                        comment=clean_comment,
                        expected_username=self._username,
                    )
                existing[filename.casefold()] = attachment
                result.attachments.append(_change(attachment))
            except ConnectorError as exc:
                raise_account_error(exc)
                result.failures.append(
                    AttachmentChangeFailure(filename=filename, error=f"{type(exc).__name__}: {exc}")
                )
                if isinstance(exc, UncertainWrite):
                    result.failures.extend(
                        AttachmentChangeFailure(
                            filename=remaining[1],
                            error="Not attempted because a previous upload has an uncertain outcome.",
                        )
                        for remaining in prepared[index + 1 :]
                    )
                    break
        return result

    async def update_attachment(
        self,
        page: str,
        attachment_id: str,
        *,
        filename: str | None = None,
        comment: str | None = None,
        media_type: str | None = None,
    ) -> AttachmentMutationResult:
        selected_id = _attachment_id(attachment_id)
        clean_filename = _filename(filename) if filename is not None else None
        clean_comment = _comment(comment)
        clean_media_type = _media_type_value(media_type) if media_type is not None else None
        if clean_filename is None and comment is None and clean_media_type is None:
            raise InvalidRequest("Supply filename, comment or media_type to change attachment metadata.")
        resolved = await self._resolve_page(page)
        await self._require_page_update(resolved.page_id)
        attachment = await self._owned_attachment(resolved.page_id, selected_id)
        if clean_filename is not None and clean_filename.casefold() != attachment.filename.casefold():
            if any(item.filename.casefold() == clean_filename.casefold() for item in await self._api.list_page_attachments(
                resolved.page_id, expected_username=self._username
            )):
                raise InvalidRequest("An attachment with that filename already exists on this page.")
        updated = await self._api.update_page_attachment(
            resolved.page_id,
            attachment,
            filename=clean_filename or attachment.filename,
            media_type=clean_media_type or attachment.media_type or _media_type(attachment.filename),
            comment=clean_comment,
            expected_username=self._username,
        )
        return AttachmentMutationResult(page_id=resolved.page_id, attachments=[_change(updated)])

    async def trash_attachments(
        self,
        page: str,
        attachment_ids: list[str],
        *,
        allow_referenced: bool = False,
    ) -> AttachmentMutationResult:
        ids = _attachment_ids(attachment_ids)
        if not isinstance(allow_referenced, bool):
            raise InvalidRequest("allow_referenced must be a boolean.")
        resolved = await self._resolve_page(page)
        attachments = {item.attachment_id: item for item in await self._api.list_page_attachments(
            resolved.page_id, expected_username=self._username
        )}
        missing = [item for item in ids if item not in attachments]
        if missing:
            raise InvalidRequest("Every attachment ID must belong to the named page.")
        page_content = await self._api.get_page_content(
            resolved.page_id, expected_username=self._username, include_storage=True
        )
        references = _referenced_attachment_ids(page_content.storage_body, list(attachments.values()))
        blocked = [item for item in ids if item in references]
        if blocked and not allow_referenced:
            raise InvalidRequest(
                "The page body still references one or more selected attachments; pass allow_referenced=true to confirm."
            )
        result = AttachmentMutationResult(
            page_id=resolved.page_id,
            notes=["References from other pages are not searched."],
        )
        for index, attachment_id in enumerate(ids):
            attachment = attachments[attachment_id]
            try:
                if not await self._api.attachment_supports_delete(
                    attachment_id, expected_username=self._username
                ):
                    raise PermissionDenied(
                        "The current account lacks the attachment delete permission; no attachment was trashed."
                    )
                await self._api.trash_page_attachment(
                    attachment_id, expected_username=self._username
                )
                result.attachments.append(_change(attachment))
            except ConnectorError as exc:
                raise_account_error(exc)
                result.failures.append(
                    AttachmentChangeFailure(
                        attachment_id=attachment_id,
                        filename=attachment.filename,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                if isinstance(exc, UncertainWrite):
                    result.failures.extend(
                        AttachmentChangeFailure(
                            attachment_id=remaining,
                            error="Not attempted because a previous trash operation has an uncertain outcome.",
                        )
                        for remaining in ids[index + 1 :]
                    )
                    break
        return result

    async def restore_attachments(
        self, page: str, attachment_ids: list[str]
    ) -> AttachmentMutationResult:
        ids = _attachment_ids(attachment_ids)
        resolved = await self._resolve_page(page)
        result = AttachmentMutationResult(page_id=resolved.page_id)
        for index, attachment_id in enumerate(ids):
            try:
                attachment = await self._api.get_page_attachment(
                    attachment_id,
                    status="trashed",
                    expected_username=self._username,
                )
                if attachment.container_page_id != resolved.page_id:
                    raise InvalidRequest("The attachment does not belong to the named page.")
                result.attachments.append(
                    _change(await self._api.restore_page_attachment(
                        attachment, expected_username=self._username
                    ))
                )
            except ConnectorError as exc:
                raise_account_error(exc)
                result.failures.append(
                    AttachmentChangeFailure(attachment_id=attachment_id, error=f"{type(exc).__name__}: {exc}")
                )
                if isinstance(exc, UncertainWrite):
                    result.failures.extend(
                        AttachmentChangeFailure(
                            attachment_id=remaining,
                            error="Not attempted because a previous restore has an uncertain outcome.",
                        )
                        for remaining in ids[index + 1 :]
                    )
                    break
        return result

    async def _resolve_page(self, page: str) -> ResolvedPageReference:
        reference = page_references(page, maximum=1)[0]
        resolved = await self._links.resolve(reference, expected_username=self._username)
        if not resolved.page_id.isdecimal() or resolved.version is not None:
            raise InvalidRequest("Attachment writes require a current page reference.")
        return resolved

    async def _require_page_update(self, page_id: str) -> None:
        if not await self._api.page_supports_update(page_id, expected_username=self._username):
            raise PermissionDenied("The current account lacks the page update permission required for attachment changes.")

    async def _owned_attachment(self, page_id: str, attachment_id: str) -> PageAttachment:
        attachments = await self._api.list_page_attachments(page_id, expected_username=self._username)
        for attachment in attachments:
            if attachment.attachment_id == attachment_id:
                detailed = await self._api.get_page_attachment(
                    attachment_id, status="current", expected_username=self._username
                )
                if detailed.container_page_id not in {None, page_id}:
                    raise InvalidRequest("The attachment ID does not belong to the named page.")
                return detailed
        raise InvalidRequest("The attachment ID does not belong to the named page.")


def _upload_paths(value: object) -> list[tuple[Path, bytes]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ATTACHMENT_MUTATIONS:
        raise InvalidRequest(f"file_paths must contain 1 to {MAX_ATTACHMENT_MUTATIONS} paths.")
    paths: list[tuple[Path, bytes]] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidRequest("file_paths contains an invalid path.")
        path = Path(item)
        if not path.is_absolute():
            raise InvalidRequest("Every attachment path must be absolute.")
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise OSError
            content = resolved.read_bytes()
        except OSError:
            raise InvalidRequest("Every attachment path must name a readable file.") from None
        paths.append((resolved, content))
    filenames = [path.name.casefold() for path, _content in paths]
    if len(filenames) != len(set(filenames)):
        raise InvalidRequest("file_paths must not contain duplicate filenames.")
    return paths


def _attachment_ids(value: object) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_ATTACHMENT_MUTATIONS:
        raise InvalidRequest(f"attachment_ids must contain 1 to {MAX_ATTACHMENT_MUTATIONS} IDs.")
    ids = [_attachment_id(item) for item in value]
    if len(ids) != len(set(ids)):
        raise InvalidRequest("attachment_ids must not contain duplicates.")
    return ids


def _attachment_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or not value.strip().isdecimal():
        raise InvalidRequest("attachment_id must be a numeric ID.")
    return value.strip()


def _filename(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or Path(value).name != value or any(ord(char) < 32 for char in value):
        raise InvalidRequest("filename must be one non-empty filename without path separators.")
    return value.strip()


def _comment(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 255 or any(ord(char) < 32 for char in value):
        raise InvalidRequest("comment must be text of at most 255 characters without control characters.")
    return value.strip()


def _media_type(value: str) -> str:
    return mimetypes.guess_type(value)[0] or "application/octet-stream"


def _media_type_value(value: object) -> str:
    if not isinstance(value, str) or "/" not in value or any(char.isspace() for char in value):
        raise InvalidRequest("media_type must be a MIME type such as image/png.")
    return value.strip().lower()


def _referenced_attachment_ids(body: str, attachments: list[PageAttachment]) -> set[str]:
    soup = BeautifulSoup(body, "html.parser")
    by_filename = {item.filename.casefold(): item.attachment_id for item in attachments}
    attachment_ids = {item.attachment_id for item in attachments}
    found: set[str] = set()
    for node in soup.find_all(True):
        attachment_id = node.get("ri:content-id") or node.get("data-linked-resource-id") or node.get("data-attachment-id")
        filename = node.get("ri:filename") or node.get("data-linked-resource-default-alias") or node.get("data-linked-resource-filename")
        if attachment_id and str(attachment_id) in attachment_ids:
            found.add(str(attachment_id))
        elif filename and str(filename).casefold() in by_filename:
            found.add(by_filename[str(filename).casefold()])
    return found


def _change(attachment: PageAttachment) -> AttachmentChange:
    return AttachmentChange(
        attachment_id=attachment.attachment_id,
        filename=attachment.filename,
        version=attachment.version,
        download_url=attachment.download_url,
        media_type=attachment.media_type,
    )
