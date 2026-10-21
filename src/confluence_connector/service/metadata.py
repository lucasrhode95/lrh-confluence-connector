"""Batch page metadata, including the complete retained version list when available."""

from __future__ import annotations

import asyncio

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
)
from confluence_connector.domain.models import PageMetadata, PageMetadataFailure, PageMetadataResult
from confluence_connector.domain.validation import page_references
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver

MAX_METADATA_PAGES = 20


class PageMetadataService:
    """Resolve and map metadata for a bounded ordered set of page references."""

    def __init__(self, api: ConfluenceApi, links: PageLinkResolver, username: str) -> None:
        self._api = api
        self._links = links
        self._username = username

    async def get_page_metadata(self, pages: list[str] | str) -> PageMetadataResult:
        """Return successful metadata and per-reference failures, preserving input order.

        The public boundary validates count and text shape before any request. Each page's source
        fields and history are mapped by the adapter; rendered page text is never parsed for metadata.
        """
        references = page_references(pages, maximum=MAX_METADATA_PAGES)
        semaphore = asyncio.Semaphore(6)

        async def read(reference: str) -> tuple[PageMetadata | None, PageMetadataFailure | None]:
            async with semaphore:
                try:
                    resolved = await self._links.resolve(reference, expected_username=self._username)
                    if resolved.version is not None:
                        raise InvalidRequest(
                            "get_page_metadata reports current page metadata; omit pageVersion from the reference."
                        )
                    page_id = resolved.page_id
                    if not page_id.isdecimal():
                        raise InvalidRequest("The selected reference did not resolve to a numeric page ID.")
                    metadata = await self._api.get_page_metadata(
                        page_id, expected_username=self._username
                    )
                    versions, truncated = await self._api.list_page_versions(
                        page_id, expected_username=self._username
                    )
                    metadata.versions = versions
                    metadata.versions_truncated = truncated
                    return metadata, None
                except ConnectorError as exc:
                    if isinstance(exc, (AccountMismatch, AuthenticationRequired)):
                        raise
                    return None, PageMetadataFailure(
                        reference=reference,
                        error=f"{type(exc).__name__}: {exc}",
                    )

        results = await asyncio.gather(*(read(reference) for reference in references))
        return PageMetadataResult(
            pages=[metadata for metadata, _ in results if metadata is not None],
            failures=[failure for _, failure in results if failure is not None],
        )
