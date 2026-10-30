"""Resolve people for search filters and exact username lookups."""

from __future__ import annotations

from typing import Any

from confluence_connector.domain.errors import InvalidRequest
from confluence_connector.domain.models import UserSearchResult
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.service import cursors

CURSOR_KIND = "find_users"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
MAX_QUERY_CHARS = 255


class UserSearch:
    """Public entry point for current-name search and exact username lookup."""

    def __init__(self, api: ConfluenceApi, username: str) -> None:
        self._api = api
        self._username = username

    async def find_users(
        self,
        query: str | None = None,
        *,
        username: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> UserSearchResult:
        """Validate one search mode, then return users mapped from source identity fields.

        Entry-point validation rejects mixed exact-name and cursor requests. Former users are
        available through the exact ``username`` route; display-name search covers current users.
        """
        if cursor is not None:
            if query is not None or username is not None or limit is not None:
                raise InvalidRequest("Pass only the cursor; it already contains the user search.")
            query, limit, start = self._resume(cursor)
        elif username is not None:
            if query is not None or limit is not None:
                raise InvalidRequest("Pass username alone for an exact user lookup.")
            exact_username = _normalize_username(username)
            user = await self._api.get_user(exact_username, expected_username=self._username)
            return UserSearchResult(
                query=exact_username,
                users=[user],
                approximate_total=1,
                start=0,
                size=1,
            )
        else:
            query = _normalize_query(query)
            limit = _limit(limit)
            start = 0

        page = await self._api.search_users_page(
            query, start=start, limit=limit, expected_username=self._username
        )
        more = page.size == limit or page.has_more is True
        next_cursor = None
        next_start = page.start + page.size
        if more and next_start <= MAX_OFFSET:
            encoded = cursors.encode(CURSOR_KIND, query=query, limit=limit, start=next_start)
            if len(encoded) <= cursors.MAX_CURSOR_CHARS:
                next_cursor = encoded
        notes = ["Former users are available only through an exact username lookup."]
        if page.total_size is not None:
            notes.append("The total is Confluence's approximate count and can change between pages.")
        if more and next_cursor is None:
            if next_start > MAX_OFFSET:
                notes.append("More results exist, but the maximum supported user-search offset was reached.")
            else:
                notes.append("More results exist, but this search cannot be continued with the current cursor size.")
        return UserSearchResult(
            query=query,
            users=page.users,
            approximate_total=page.total_size,
            start=page.start,
            size=page.size,
            next_cursor=next_cursor,
            notes=notes,
        )

    def _resume(self, cursor: str) -> tuple[str, int, int]:
        state = cursors.decode(cursor, CURSOR_KIND)
        query = _normalize_query(state.get("query"))
        limit = _limit(state.get("limit"))
        start = _offset(state.get("start"))
        return query, limit, start


def _normalize_query(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_CHARS:
        raise InvalidRequest(f"query must contain 1 to {MAX_QUERY_CHARS} characters.")
    if any(ord(char) < 32 for char in value):
        raise InvalidRequest("query cannot contain control characters.")
    return " ".join(value.split())


def _normalize_username(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_QUERY_CHARS:
        raise InvalidRequest(f"username must contain 1 to {MAX_QUERY_CHARS} characters.")
    if any(ord(char) < 32 for char in value):
        raise InvalidRequest("username cannot contain control characters.")
    return value.strip()


def _limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise InvalidRequest(f"limit must be an integer from 1 through {MAX_LIMIT}.")
    return value


def _offset(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_OFFSET:
        raise InvalidRequest(f"user search offset must be an integer from 0 through {MAX_OFFSET}.")
    return value
