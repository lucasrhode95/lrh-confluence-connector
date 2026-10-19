"""Lazy process-scoped composition root for Confluence services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from confluence_connector import config
from confluence_connector.auth.store import TokenStore
from confluence_connector.domain.errors import AccountMismatch, AuthenticationRequired
from confluence_connector.domain.models import AuthStatus, CurrentUser
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.remote.transport import ConfluenceTransport
from confluence_connector.service.attachments import AttachmentDownloads
from confluence_connector.service.metadata import PageMetadataService
from confluence_connector.service.pages import PageTextService
from confluence_connector.service.search import ContentSearch
from confluence_connector.service.spaces import SpaceService
from confluence_connector.service.timezone_calibration import (
    CqlTimezoneCalibration,
    calibrate_cql_timezone as _calibrate_cql_timezone,
)
from confluence_connector.service.tree import PageTree
from confluence_connector.service.users import UserSearch


@dataclass
class Services:
    """Adapters and services bound to the account observed for this tool call."""

    username: str
    api: ConfluenceApi
    links: PageLinkResolver
    spaces: SpaceService
    search: ContentSearch
    users: UserSearch
    tree: PageTree
    metadata: PageMetadataService
    pages: PageTextService
    attachments: AttachmentDownloads


class AppContext:
    """Build one async HTTP client lazily and refresh bound services after account changes."""

    def __init__(
        self,
        settings: dict[str, Any] | None = None,
        *,
        store: TokenStore | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Settings are validated by the CLI config boundary; injected clients are caller-owned."""
        self.settings = settings if settings is not None else config.load_config()
        self.store = store or TokenStore()
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._transport: ConfluenceTransport | None = None
        self._services: Services | None = None
        self._lock = asyncio.Lock()

    async def services(self) -> Services:
        """Return adapters bound to the currently stored username; never memoize token values."""
        credential = self.store.load()
        if credential is None:
            raise AuthenticationRequired("No Confluence token is stored.")
        if self._services is not None and self._services.username.casefold() == credential.username.casefold():
            return self._services
        async with self._lock:
            credential = self.store.load()
            if credential is None:
                raise AuthenticationRequired("No Confluence token is stored.")
            if self._services is None or self._services.username.casefold() != credential.username.casefold():
                self._services = self._build(credential.username)
            return self._services

    async def auth_status(self) -> AuthStatus:
        """Return offline status even when no credential is stored."""
        credential = self.store.load()
        return AuthStatus(
            configured=True,
            token_stored=credential is not None,
            username=credential.username if credential else None,
            base_url=str(self.settings["base_url"]),
        )

    async def check_auth_status(self) -> AuthStatus:
        """Verify the current encrypted credential using the documented current-user endpoint."""
        credential = self.store.load()
        if credential is None:
            raise AuthenticationRequired("No Confluence token is stored.")
        api = self._api()
        user = await api.current_user(expected_username=credential.username)
        if user.username.casefold() != credential.username.casefold():
            raise AccountMismatch("Confluence returned a different user than the encrypted credential records.")
        return AuthStatus(
            configured=True,
            token_stored=True,
            username=user.username,
            base_url=str(self.settings["base_url"]),
            checked=True,
        )

    async def calibrate_cql_timezone(self) -> CqlTimezoneCalibration:
        """Measure CQL's timezone after the caller has verified the active stored account."""
        credential = self.store.load()
        if credential is None:
            raise AuthenticationRequired("No Confluence token is stored.")
        return await _calibrate_cql_timezone(self._api(), credential.username)

    async def verify_token(self, token: str) -> CurrentUser:
        """Verify a prompted candidate before the caller persists it; no credential lock is held in flight."""
        if not token.strip():
            raise AuthenticationRequired("The Confluence token cannot be empty.")
        return await self._api().current_user(token_override=token.strip())

    async def aclose(self) -> None:
        """Close the owned process HTTP client, if one has been created."""
        if self._transport is not None:
            await self._transport.aclose()
        elif self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()

    def _build(self, username: str) -> Services:
        api = self._api()
        links = PageLinkResolver(self.settings, self._transport_or_raise(), api)
        return Services(
            username=username,
            api=api,
            links=links,
            spaces=SpaceService(self.settings, self.store, api, username),
            search=ContentSearch(api, links, username, self.settings.get("cql_timezone")),
            users=UserSearch(api, username),
            tree=PageTree(api, links, username),
            metadata=PageMetadataService(api, links, username),
            pages=PageTextService(api, links, username, str(self.settings["base_url"])),
            attachments=AttachmentDownloads(api, links, username),
        )

    def _api(self) -> ConfluenceApi:
        return ConfluenceApi(self.settings, self._transport_or_raise())

    def _transport_or_raise(self) -> ConfluenceTransport:
        if self._transport is None:
            self._transport = ConfluenceTransport(
                self.settings, self.store, client=self._http_client
            )
        return self._transport
