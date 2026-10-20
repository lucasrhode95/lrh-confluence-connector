"""Visible-space capability."""

from __future__ import annotations

from confluence_connector.auth.store import TokenStore
from confluence_connector.domain.errors import AccountMismatch, AuthenticationRequired
from confluence_connector.domain.models import SpaceList
from confluence_connector.remote.confluence import ConfluenceApi


class SpaceService:
    """List spaces for the account selected when the service was built."""

    def __init__(self, store: TokenStore, api: ConfluenceApi, username: str) -> None:
        self._store = store
        self._api = api
        self._username = username

    async def list_spaces(self) -> SpaceList:
        """List visible spaces for the account selected when this service was built."""
        credential = self._store.load()
        if credential is None:
            raise AuthenticationRequired("No Confluence token is stored.")
        if credential.username.casefold() != self._username.casefold():
            raise AccountMismatch("The active Confluence account changed; retry the request.")
        return await self._api.list_spaces(expected_username=self._username)
