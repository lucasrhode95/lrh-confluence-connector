"""Visible-space capability."""

from __future__ import annotations

from confluence_connector.domain.models import SpaceList
from confluence_connector.remote.confluence import ConfluenceApi


class SpaceService:
    """List spaces for the account selected when the service was built."""

    def __init__(self, api: ConfluenceApi, username: str) -> None:
        self._api = api
        self._username = username

    async def list_spaces(self) -> SpaceList:
        """List visible spaces for the account selected when this service was built."""
        return await self._api.list_spaces(expected_username=self._username)
