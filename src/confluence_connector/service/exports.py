"""Full-page exports into timestamped local files and flat ZIP archives."""

from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from confluence_connector.domain.errors import (
    ConnectorError,
    InvalidRequest,
)
from confluence_connector.domain.models import (
    PageAttachment,
    PageComment,
    PageExportResult,
    PageParseScope,
    PageReadRecord,
    PageTextFormat,
    ResolvedPageReference,
    SavedAttachment,
)
from confluence_connector.domain.validation import page_references
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service import localfiles
from confluence_connector.service.attachments import AttachmentDownloads
from confluence_connector.service.errors import raise_account_error
from confluence_connector.service.filenames import (
    MAX_FILENAME_COMPONENT_UNITS,
    filename_component_units,
    safe_filename_stem,
    sanitize_filename_component,
)
from confluence_connector.service.pages import (
    PageTextService,
    _format,
    _parse_scope,
)

MAX_EXPORT_PAGES = 500
PAGE_READ_CONCURRENCY = 2
ATTACHMENT_DOWNLOAD_CONCURRENCY = 6
MAX_EXPORT_MEMBER_LENGTH = 180
MAX_SINGLE_FILENAME_LENGTH = 160


@dataclass(frozen=True)
class _ReportItem:
    category: str
    subject: str
    reason: str


@dataclass(frozen=True)
class _PageMetadata:
    page_id: str
    title: str
    version: int


@dataclass
class _PageExport:
    reference: str
    metadata: _PageMetadata | None = None
    staged_file: Path | None = None
    attachment_directory: Path | None = None
    failure: str | None = None
    downloaded_attachments: int = 0
    attachment_failures: int = 0
    report_items: list[_ReportItem] = field(default_factory=list)


class PageExports:
    """Resolve, read and package pages while keeping remote and file concurrency bounded."""

    def __init__(
        self,
        api: ConfluenceApi,
        links: PageLinkResolver,
        pages: PageTextService,
        attachments: AttachmentDownloads,
        username: str,
    ) -> None:
        self._api = api
        self._links = links
        self._pages = pages
        self._attachments = attachments
        self._username = username

    async def export_pages(
        self,
        pages: str | list[str],
        *,
        format: PageTextFormat | None = None,
        parse_scope: PageParseScope | dict[str, Any] | None = None,
        include_attachments: bool = False,
    ) -> PageExportResult:
        """Validate all options before requests, then write the complete export locally.

        The public boundary validates references, format, scope and the attachment switch. Internal
        readers receive validated references and a numeric page ID; a provenance header is always
        enabled because D8 requires it in exported files.
        """
        references = page_references(pages, maximum=MAX_EXPORT_PAGES)
        page_format = _format(format)
        scope = _parse_scope(parse_scope).model_copy(update={"header": True})
        include_file_bytes = _boolean(include_attachments)
        output_directory = self._output_directory()

        with tempfile.TemporaryDirectory(
            prefix=".export-", dir=output_directory
        ) as temporary:
            staging = Path(temporary)
            outcomes = await self._read_in_batches(
                references,
                staging,
                page_format=page_format,
                parse_scope=scope,
                include_attachments=include_file_bytes,
            )
            report_items = [
                item for outcome in outcomes for item in outcome.report_items
            ]
            exported_pages = sum(outcome.metadata is not None for outcome in outcomes)
            failed_pages = sum(outcome.failure is not None for outcome in outcomes)
            exported_attachments = sum(
                outcome.downloaded_attachments for outcome in outcomes
            )
            failed_attachments = sum(
                outcome.attachment_failures for outcome in outcomes
            )

            if (
                len(outcomes) == 1
                and outcomes[0].metadata is not None
                and not include_file_bytes
                and not report_items
            ):
                target = self._write_single(outcomes[0], output_directory, page_format)
            else:
                target = self._write_zip(
                    outcomes,
                    report_items,
                    output_directory,
                    page_format=page_format,
                )

        return PageExportResult(
            path=str(target),
            filename=target.name,
            content_type=_content_type(page_format, target.suffix),
            size_bytes=target.stat().st_size,
            pages_exported=exported_pages,
            pages_failed=failed_pages,
            attachments_exported=exported_attachments,
            attachments_failed=failed_attachments,
        )

    async def _read_in_batches(
        self,
        references: list[str],
        staging: Path,
        *,
        page_format: PageTextFormat,
        parse_scope: PageParseScope,
        include_attachments: bool,
    ) -> list[_PageExport]:
        outcomes: list[_PageExport] = []
        for start in range(0, len(references), PAGE_READ_CONCURRENCY):
            tasks = [
                asyncio.create_task(
                    self._export_one(
                        index,
                        reference,
                        staging,
                        page_format=page_format,
                        parse_scope=parse_scope,
                        include_attachments=include_attachments,
                    )
                )
                for index, reference in enumerate(
                    references[start : start + PAGE_READ_CONCURRENCY], start=start
                )
            ]
            try:
                outcomes.extend(await asyncio.gather(*tasks))
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        return outcomes

    async def _export_one(
        self,
        index: int,
        reference: str,
        staging: Path,
        *,
        page_format: PageTextFormat,
        parse_scope: PageParseScope,
        include_attachments: bool,
    ) -> _PageExport:
        outcome = _PageExport(reference=reference)
        try:
            resolved: ResolvedPageReference = await self._links.resolve(
                reference, expected_username=self._username
            )
            if not resolved.page_id.isdecimal():
                raise InvalidRequest(
                    "The selected reference did not resolve to a numeric page ID."
                )
            page_id = resolved.page_id
        except ConnectorError as exc:
            raise_account_error(exc)
            outcome.failure = _error_text(exc)
            outcome.report_items.append(_ReportItem("page", reference, outcome.failure))
            return outcome

        attachment_override: list[PageAttachment] | None = None
        if include_attachments:
            try:
                attachment_override = await self._api.list_page_attachments(
                    page_id, expected_username=self._username
                )
                if len(attachment_override) == 500:
                    outcome.report_items.append(
                        _ReportItem(
                            "attachment list",
                            _page_label(page_id, None),
                            "The attachment list reached its 500-item request limit and may be incomplete.",
                        )
                    )
            except ConnectorError as exc:
                raise_account_error(exc)
                attachment_override = []
                outcome.attachment_failures += 1
                outcome.report_items.append(
                    _ReportItem(
                        "attachment list",
                        _page_label(page_id, None),
                        _error_text(exc),
                    )
                )

        try:
            record = await self._pages.read_full_page(
                page_id,
                version=resolved.version,
                page_format=page_format,
                parse_scope=parse_scope,
                attachment_override=attachment_override,
            )
        except ConnectorError as exc:
            raise_account_error(exc)
            outcome.failure = _error_text(exc)
            outcome.report_items.append(
                _ReportItem("page", _page_label(page_id, None), outcome.failure)
            )
            return outcome

        outcome.report_items.extend(
            _ReportItem(
                "page section",
                _page_label(record.page_id, record.title),
                _single_line(note),
            )
            for note in record.notes
        )
        extension = ".txt" if page_format == "text" else ".html"
        outcome.staged_file = staging / f"page-{index:04d}{extension}"
        descriptor = os.open(outcome.staged_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(_page_content(record, page_format, parse_scope).encode("utf-8"))

        if include_attachments:
            outcome.attachment_directory = staging / f"attachments-{index:04d}"
            outcome.attachment_directory.mkdir()
            await self._download_attachments(record, outcome)
        outcome.metadata = _PageMetadata(
            page_id=record.page_id,
            title=record.title,
            version=record.version,
        )
        return outcome

    async def _download_attachments(
        self, record: PageReadRecord, outcome: _PageExport
    ) -> None:
        directory = outcome.attachment_directory
        if not record.attachments or directory is None:
            return
        for start in range(0, len(record.attachments), ATTACHMENT_DOWNLOAD_CONCURRENCY):
            batch = record.attachments[start : start + ATTACHMENT_DOWNLOAD_CONCURRENCY]
            downloads = [
                asyncio.create_task(
                    self._attachments.download_to_directory(attachment, directory)
                )
                for attachment in batch
            ]
            results = await asyncio.gather(*downloads, return_exceptions=True)
            for attachment, result in zip(batch, results, strict=True):
                if isinstance(result, SavedAttachment):
                    outcome.downloaded_attachments += 1
                    continue
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, ConnectorError):
                    raise_account_error(result)
                    reason = _error_text(result)
                elif isinstance(result, OSError):
                    reason = f"LocalFileError: {type(result).__name__}"
                elif isinstance(result, BaseException):
                    raise result
                else:
                    raise TypeError(
                        "Attachment download returned an unexpected result."
                    )
                outcome.attachment_failures += 1
                outcome.report_items.append(
                    _ReportItem(
                        "attachment",
                        f"{_page_label(record.page_id, record.title)} — {attachment.filename}",
                        reason,
                    )
                )

    def _write_single(
        self,
        outcome: _PageExport,
        output_directory: Path,
        page_format: PageTextFormat,
    ) -> Path:
        assert outcome.metadata is not None and outcome.staged_file is not None
        prefix = f"{_timestamp()} - "
        page_name = _page_filename(
            outcome.metadata,
            page_format,
            maximum_length=MAX_SINGLE_FILENAME_LENGTH - len(prefix),
            maximum_component_units=MAX_FILENAME_COMPONENT_UNITS
            - filename_component_units(prefix),
        )
        filename = f"{prefix}{page_name}"
        target = localfiles.claim(output_directory, filename)
        try:
            outcome.staged_file.replace(target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    def _write_zip(
        self,
        outcomes: list[_PageExport],
        report_items: list[_ReportItem],
        output_directory: Path,
        *,
        page_format: PageTextFormat,
    ) -> Path:
        target = localfiles.claim(
            output_directory, f"confluence-export-{_timestamp()}.zip"
        )
        used_names: set[str] = set()
        try:
            with zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for outcome in outcomes:
                    if outcome.metadata is None or outcome.staged_file is None:
                        continue
                    page_name = _unique_archive_name(
                        _page_filename(outcome.metadata, page_format), used_names
                    )
                    archive.write(outcome.staged_file, arcname=page_name)
                    if outcome.attachment_directory is None:
                        continue
                    folder_name = Path(page_name).stem
                    for file_path in sorted(outcome.attachment_directory.iterdir()):
                        if file_path.is_file():
                            archive.write(
                                file_path, arcname=f"{folder_name}/{file_path.name}"
                            )
                if report_items:
                    archive.writestr("_export-report.txt", _export_report(report_items))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    def _output_directory(self) -> Path:
        fingerprint = hashlib.sha256(
            self._username.casefold().encode("utf-8")
        ).hexdigest()[:16]
        return localfiles.private_dir(localfiles.kept_dir("exports") / fingerprint)


def _page_content(
    record: PageReadRecord,
    page_format: PageTextFormat,
    parse_scope: PageParseScope,
) -> str:
    if page_format == "text":
        return record.text
    metadata = (
        record.header
        or f"# {record.title}\nPage ID: {record.page_id}\nVersion: {record.version}"
    )
    metadata = metadata.replace("--", "—")
    body = f"<!--\n{metadata}\n-->\n{record.text}"
    if parse_scope.comments:
        body += _html_comments(record.comments)
    return body


def _html_comments(comments: list[PageComment]) -> str:
    parts = ['\n<section class="confluence-comments"><h2>Comments</h2>']
    for comment in comments:
        details = [
            f"@{comment.author.display_name}"
            if comment.author and comment.author.display_name
            else "Unknown author"
        ]
        if comment.created_at:
            details.append(comment.created_at)
        if comment.location:
            details.append(comment.location)
        if comment.resolution:
            details.append(comment.resolution)
        parts.append("<article><p><strong>")
        parts.append(html.escape(" — ".join(details), quote=True))
        parts.append("</strong></p>")
        if comment.selected_text:
            parts.append("<p>Selected text: ")
            parts.append(html.escape(comment.selected_text, quote=True))
            parts.append("</p>")
        if comment.body:
            parts.append("<blockquote><p>")
            parts.append(html.escape(comment.body, quote=True).replace("\n", "<br>\n"))
            parts.append("</p></blockquote>")
        parts.append("</article>")
    parts.append("</section>\n")
    return "".join(parts)


def _page_filename(
    metadata: _PageMetadata,
    page_format: PageTextFormat,
    *,
    maximum_length: int = MAX_EXPORT_MEMBER_LENGTH,
    maximum_component_units: int = MAX_FILENAME_COMPONENT_UNITS,
) -> str:
    extension = ".txt" if page_format == "text" else ".html"
    suffix = f" ({metadata.page_id}) v{metadata.version}{extension}"
    if len(suffix) >= maximum_length or filename_component_units(suffix) >= maximum_component_units:
        raise InvalidRequest("The resolved page ID is too long for a safe export filename.")
    title = sanitize_filename_component(metadata.title) or "Untitled page"
    title = safe_filename_stem(
        title,
        suffix=suffix,
        maximum_length=maximum_length,
        maximum_component_units=maximum_component_units,
        fallback="Page",
    )
    filename = f"{title}{suffix}"
    if len(filename) > maximum_length or filename_component_units(filename) > maximum_component_units:
        raise InvalidRequest("The resolved page ID is too long for a safe export filename.")
    return filename


def _unique_archive_name(filename: str, used_names: set[str]) -> str:
    if filename.casefold() not in used_names:
        used_names.add(filename.casefold())
        return filename
    path = Path(filename)
    match = re.search(r" \(\d+\) v\d+$", path.stem)
    if match:
        title = path.stem[: match.start()]
        identity = path.stem[match.start() :]
    else:
        title = path.stem
        identity = ""
    counter = 2
    while True:
        duplicate_suffix = f" ({counter})"
        tail = f"{duplicate_suffix}{identity}{path.suffix}"
        title_limit = MAX_EXPORT_MEMBER_LENGTH - len(tail)
        candidate_title = title[: max(0, title_limit)].rstrip(" .")
        while candidate_title and (
            len(candidate_title + tail) > MAX_EXPORT_MEMBER_LENGTH
            or filename_component_units(candidate_title + tail) > MAX_FILENAME_COMPONENT_UNITS
        ):
            candidate_title = candidate_title[:-1].rstrip(" .")
        candidate_title = candidate_title or "Page"
        candidate = f"{candidate_title}{tail}"
        if candidate.casefold() not in used_names:
            if (
                len(candidate) > MAX_EXPORT_MEMBER_LENGTH
                or filename_component_units(candidate) > MAX_FILENAME_COMPONENT_UNITS
            ):
                raise InvalidRequest("The resolved page ID is too long for a safe export filename.")
            used_names.add(candidate.casefold())
            return candidate
        counter += 1


def _export_report(items: list[_ReportItem]) -> str:
    lines = ["Confluence export report", "", "Failed or incomplete items:"]
    for item in items:
        lines.append(
            f"- {item.category}: {_single_line(item.subject)} — {_single_line(item.reason)}"
        )
    return "\n".join(lines) + "\n"


def _page_label(page_id: str, title: str | None) -> str:
    return f"Page {page_id}" if not title else f"Page {page_id} ({_single_line(title)})"


def _error_text(error: ConnectorError) -> str:
    return f"{type(error).__name__}: {_single_line(str(error))}"


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequest("include_attachments must be a boolean.")
    return value


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _content_type(page_format: PageTextFormat, suffix: str) -> str:
    if suffix == ".zip":
        return "application/zip"
    return (
        "text/plain; charset=utf-8"
        if page_format == "text"
        else "text/html; charset=utf-8"
    )
