"""Authentication status and visible-space capabilities."""

from __future__ import annotations

from typing import Any

from confluence_connector.auth.store import TokenStore
from confluence_connector.domain.errors import AccountMismatch, AuthenticationRequired
from confluence_connector.domain.models import AuthStatus, SpaceList
from confluence_connector.remote.confluence import ConfluenceApi


class SpaceService:
    """Coordinate one validated account with the Confluence REST adapter."""

    def __init__(self, settings: dict[str, Any], store: TokenStore, api: ConfluenceApi, username: str) -> None:
        self._settings = settings
        self._store = store
        self._api = api
        self._username = username

    def auth_status(self) -> AuthStatus:
        """Report the locally stored account without contacting Confluence."""
        credential = self._store.load()
        return AuthStatus(
            configured=True,
            token_stored=credential is not None,
            username=credential.username if credential else None,
            base_url=str(self._settings["base_url"]),
        )

    async def check_auth_status(self) -> AuthStatus:
        """Verify the stored token against the configured current-user endpoint."""
        credential = self._store.load()
        if credential is None:
            raise AuthenticationRequired("No Confluence token is stored.")
        current_user = await self._api.current_user(expected_username=credential.username)
        if current_user.username.casefold() != credential.username.casefold():
            raise AccountMismatch("Confluence returned a different account than the one bound to the stored token.")
        return AuthStatus(
            configured=True,
            token_stored=True,
            username=current_user.username,
            base_url=str(self._settings["base_url"]),
            checked=True,
        )

    async def list_spaces(self) -> SpaceList:
        """List visible spaces for the account selected when this service was built."""
        credential = self._store.load()
        if credential is None:
            raise AuthenticationRequired("No Confluence token is stored.")
        if credential.username.casefold() != self._username.casefold():
            raise AccountMismatch("The active Confluence account changed; retry the request.")
        return await self._api.list_spaces(expected_username=self._username)
