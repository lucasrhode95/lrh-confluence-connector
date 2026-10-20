"""Validated content search, query construction and stateless result paging."""

from __future__ import annotations

import unicodedata
from typing import Any

from pydantic import ValidationError

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    ConnectorError,
    InvalidRequest,
    Upstream,
)
from confluence_connector.domain.models import (
    ContentSearchHit,
    ContentSearchResult,
    SearchFields,
    SearchMatch,
    SearchQuery,
    SearchScope,
)
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service.cql_dates import cql_date_literal, normalize_scope_time
from confluence_connector.service import cursors

CURSOR_KIND = "search_content"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
MAX_QUERY_CHARS = 1_000
MAX_SCOPE_VALUES = 50
MAX_SCOPE_VALUE_CHARS = 255
QUICKNAV_LIMIT = 20


class ContentSearch:
    """Public search entry point; validates tool input before constructing CQL or making requests."""

    def __init__(
        self,
        api: ConfluenceApi,
        links: PageLinkResolver,
        username: str,
        cql_timezone: str | None = None,
    ) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._cql_timezone = cql_timezone

    async def search_content(
        self,
        query: str | None = None,
        *,
        scope: SearchScope | dict[str, Any] | None = None,
        match: SearchMatch | None = None,
        fields: SearchFields | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContentSearchResult:
        """Return one CQL page, optionally supplementing title searches with quick-navigation hits.

        Entry point: validates query, scope, modes, limit and cursor. A cursor carries the complete
        normalized request and must be passed alone. Internal query builders trust this contract.
        """
        if cursor is not None:
            if any(value is not None for value in (query, scope, match, fields, limit)):
                raise InvalidRequest("Pass only the cursor; it already contains the search and scope.")
            query, match, fields, normalized_scope, limit, start, seen_ids = self._resume(cursor)
        else:
            if query is None:
                raise InvalidRequest("Pass a query, or only the next_cursor of an earlier search.")
            query, terms = _normalize_query(query)
            match = _search_match(match)
            fields = _search_fields(fields)
            limit = _limit(limit)
            normalized_scope = await self._validate_scope(scope)
            start, seen_ids = 0, set()
        terms = _search_terms(query)
        query_terms = _title_terms(terms) if fields == "title" else terms
        if not query_terms:
            raise InvalidRequest("query must contain searchable text after punctuation handling.")
        cql = _build_cql(query_terms, match, fields, normalized_scope, self._cql_timezone)
        page = await self._api.search_content_page(
            cql,
            start=start,
            limit=limit,
            include_archived=normalized_scope.include_archived,
            expected_username=self._username,
        )

        quicknav_hits: list[ContentSearchHit] = []
        queries_sent = [SearchQuery(source="cql", value=cql)]
        notes: list[str] = []
        next_seen_ids = set(seen_ids)
        quicknav_allowed = _quicknav_allowed(normalized_scope)
        if fields == "title" and start == 0 and quicknav_allowed:
            title_query = " ".join(query_terms)
            queries_sent.append(SearchQuery(source="quicknav", value=title_query))
            try:
                quicknav_hits = await self._api.quicknav_title_matches(
                    title_query,
                    limit=min(QUICKNAV_LIMIT, limit),
                    expected_username=self._username,
                )
            except (AccountMismatch, AuthenticationRequired):
                raise
            except ConnectorError as exc:
                notes.append(
                    "Quick navigation was unavailable ("
                    f"{type(exc).__name__}); CQL title matches are returned."
                )
        hits = _merge_hits(page.hits, quicknav_hits, seen_ids)[:limit]
        next_seen_ids.update(
            hit.content_id
            for hit in hits
            if hit.content_id is not None and "quicknav" in hit.sources
        )
        next_start = page.start + page.size
        # ``totalSize`` is approximate on this endpoint. A full server page is the
        # reliable signal to offer a continuation; an extra empty page is harmless.
        more = page.size == limit
        next_cursor = None
        if more and next_start <= MAX_OFFSET:
            encoded = cursors.encode(
                CURSOR_KIND,
                query=query,
                match=match,
                fields=fields,
                scope=normalized_scope.model_dump(mode="json"),
                limit=limit,
                start=next_start,
                seen_quicknav_ids=sorted(next_seen_ids),
            )
            if len(encoded) <= cursors.MAX_CURSOR_CHARS:
                next_cursor = encoded
            else:
                notes.append("More results exist, but this search scope is too large to continue safely.")
        elif more:
            notes.append("More results exist, but the maximum supported search offset was reached.")
        if page.total_size is not None:
            notes.append("The total is Confluence's approximate count and can change between pages.")
        if fields == "title" and quicknav_allowed:
            notes.append("The first page can include additional title matches from quick navigation.")
        elif fields == "title" and start == 0:
            notes.append("Quick navigation was skipped so its unfiltered results cannot bypass scope filters.")
        return ContentSearchResult(
            query=query,
            match=match,
            fields=fields,
            scope=normalized_scope,
            queries_sent=queries_sent,
            hits=hits,
            start=page.start,
            size=page.size,
            approximate_total=page.total_size,
            more_results_available=more,
            next_cursor=next_cursor,
            notes=notes,
        )

    async def _validate_scope(self, value: SearchScope | dict[str, Any] | None) -> SearchScope:
        try:
            source = {} if value is None else value
            scope = (
                source.model_copy(deep=True)
                if isinstance(source, SearchScope)
                else SearchScope.model_validate(source)
            )
        except ValidationError:
            raise InvalidRequest("scope contains an unsupported field or value.") from None
        for name in (
            "space_keys",
            "content_types",
            "labels",
            "creator_usernames",
            "contributor_usernames",
        ):
            values = getattr(scope, name)
            if values is not None:
                setattr(scope, name, _scope_values(name, values))
        scope.created_after = normalize_scope_time("created_after", scope.created_after)
        scope.created_before = normalize_scope_time("created_before", scope.created_before)
        scope.modified_after = normalize_scope_time("modified_after", scope.modified_after)
        scope.modified_before = normalize_scope_time("modified_before", scope.modified_before)
        _check_range("created", scope.created_after, scope.created_before)
        _check_range("modified", scope.modified_after, scope.modified_before)
        if scope.parent is not None:
            scope.parent = (await self._links.resolve(scope.parent, expected_username=self._username)).page_id
        if scope.ancestor is not None:
            scope.ancestor = (await self._links.resolve(scope.ancestor, expected_username=self._username)).page_id
        return scope

    def _resume(
        self, cursor: str
    ) -> tuple[str, SearchMatch, SearchFields, SearchScope, int, int, set[str]]:
        """Decode and revalidate a cursor; cursors are opaque encoding, not signed authorization."""
        state = cursors.decode(cursor, CURSOR_KIND)
        try:
            query, terms = _normalize_query(state.get("query"))
            match = _search_match(state.get("match"))
            fields = _search_fields(state.get("fields"))
            scope = SearchScope.model_validate(state.get("scope"))
        except ValidationError:
            raise InvalidRequest("The search cursor contains an invalid scope.") from None
        limit = _limit(state.get("limit"))
        start = _offset(state.get("start"))
        seen_ids = _cursor_ids(state.get("seen_quicknav_ids"))
        if not terms:
            raise InvalidRequest("The search cursor contains no searchable text.")
        scope = _validate_resumed_scope(scope)
        return query, match, fields, scope, limit, start, seen_ids


def _normalize_query(value: Any) -> tuple[str, list[str]]:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_CHARS:
        raise InvalidRequest(f"query must contain 1 to {MAX_QUERY_CHARS} characters.")
    if any(ord(char) < 32 for char in value):
        raise InvalidRequest("query cannot contain control characters.")
    terms = _search_terms(value)
    if not terms:
        raise InvalidRequest("query must contain searchable text after punctuation handling.")
    return " ".join(terms), terms


def _search_terms(query: str) -> list[str]:
    """Split only on whitespace and drop punctuation standing alone or at either term edge."""
    terms: list[str] = []
    for raw in query.split():
        first, last = 0, len(raw)
        while first < last and _is_punctuation(raw[first]):
            first += 1
        while last > first and _is_punctuation(raw[last - 1]):
            last -= 1
        term = raw[first:last]
        if term:
            terms.append(term)
    return terms


def _title_terms(terms: list[str]) -> list[str]:
    title_text = " ".join("".join(" " if _is_punctuation(char) else char for char in term) for term in terms)
    return [term for term in title_text.split() if term]


def _is_punctuation(value: str) -> bool:
    return unicodedata.category(value).startswith("P")


def _search_match(value: Any) -> SearchMatch:
    if value is None:
        return "all"
    if not isinstance(value, str) or value not in {"all", "any", "phrase"}:
        raise InvalidRequest("match must be all, any or phrase.")
    return value


def _search_fields(value: Any) -> SearchFields:
    if value is None:
        return "all"
    if not isinstance(value, str) or value not in {"all", "title"}:
        raise InvalidRequest("fields must be all or title.")
    return value


def _limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise InvalidRequest(f"limit must be an integer from 1 through {MAX_LIMIT}.")
    return value


def _offset(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_OFFSET:
        raise InvalidRequest(f"search offset must be an integer from 0 through {MAX_OFFSET}.")
    return value


def _scope_values(name: str, values: list[str]) -> list[str]:
    if not values or len(values) > MAX_SCOPE_VALUES:
        raise InvalidRequest(f"scope.{name} must contain 1 to {MAX_SCOPE_VALUES} values.")
    normalized: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > MAX_SCOPE_VALUE_CHARS
            or any(ord(char) < 32 for char in value)
        ):
            raise InvalidRequest(f"scope.{name} contains an invalid value.")
        if value.strip() not in normalized:
            normalized.append(value.strip())
    return normalized


def _validate_resumed_scope(scope: SearchScope) -> SearchScope:
    """Reapply scalar and collection validation to untrusted, self-contained cursor state."""
    scope = scope.model_copy(deep=True)
    for name in (
        "space_keys",
        "content_types",
        "labels",
        "creator_usernames",
        "contributor_usernames",
    ):
        values = getattr(scope, name)
        if values is not None:
            setattr(scope, name, _scope_values(name, values))
    for name in ("created_after", "created_before", "modified_after", "modified_before"):
        setattr(scope, name, normalize_scope_time(name, getattr(scope, name)))
    _check_range("created", scope.created_after, scope.created_before)
    _check_range("modified", scope.modified_after, scope.modified_before)
    for name in ("parent", "ancestor"):
        value = getattr(scope, name)
        if value is not None and (not value.isdecimal() or len(value) > 255):
            raise InvalidRequest(f"The search cursor contains an invalid scope.{name}.")
    return scope


def _check_range(name: str, after: str | None, before: str | None) -> None:
    if after is not None and before is not None and after > before:
        raise InvalidRequest(f"scope.{name}_after must not be later than scope.{name}_before.")


def _build_cql(
    terms: list[str],
    match: SearchMatch,
    fields: SearchFields,
    scope: SearchScope,
    cql_timezone: str | None = None,
) -> str:
    search_field = "title" if fields == "title" else "siteSearch"
    escaped_terms = [_text_search_term(term) for term in terms]
    if match == "all":
        clauses = [f"{search_field} ~ {_cql_literal(term)}" for term in escaped_terms]
    elif match == "any":
        clauses = [f"{search_field} ~ {_cql_literal(' '.join(escaped_terms))}"]
    else:
        phrase = '"' + " ".join(escaped_terms) + '"'
        clauses = [f"{search_field} ~ {_cql_literal(phrase)}"]
    if scope.space_keys:
        clauses.append("space IN (" + ", ".join(_cql_literal(key) for key in scope.space_keys) + ")")
    if scope.space_type is not None:
        clauses.append(f"space.type = {scope.space_type}")
    if scope.content_types:
        clauses.append("type IN (" + ", ".join(scope.content_types) + ")")
    if scope.labels:
        clauses.append("label IN (" + ", ".join(_cql_literal(label) for label in scope.labels) + ")")
    if scope.creator_usernames:
        clauses.append(
            "creator IN (" + ", ".join(_cql_literal(name) for name in scope.creator_usernames) + ")"
        )
    if scope.contributor_usernames:
        clauses.append(
            "contributor IN (" + ", ".join(_cql_literal(name) for name in scope.contributor_usernames) + ")"
        )
    if scope.created_after:
        date_value = cql_date_literal("created_after", scope.created_after, "after", cql_timezone)
        clauses.append(f"created >= {_cql_literal(date_value)}")
    if scope.created_before:
        date_value = cql_date_literal("created_before", scope.created_before, "before", cql_timezone)
        clauses.append(f"created <= {_cql_literal(date_value)}")
    if scope.modified_after:
        date_value = cql_date_literal("modified_after", scope.modified_after, "after", cql_timezone)
        clauses.append(f"lastmodified >= {_cql_literal(date_value)}")
    if scope.modified_before:
        date_value = cql_date_literal("modified_before", scope.modified_before, "before", cql_timezone)
        clauses.append(f"lastmodified <= {_cql_literal(date_value)}")
    if scope.parent is not None:
        clauses.append(f"parent = {scope.parent}")
    if scope.ancestor is not None:
        clauses.append(f"ancestor = {scope.ancestor}")
    cql = " AND ".join(clauses)
    if len(cql) > 8_000:
        raise InvalidRequest("The search query and scope produce an overly long CQL request.")
    return cql


def _cql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _text_search_term(value: str) -> str:
    """Escape Lucene-style text syntax before the containing CQL string is escaped."""
    reserved = frozenset(r'+-&|!(){}[]^~*?\:/"')
    return "".join("\\\\" + char if char in reserved else char for char in value)


def _quicknav_allowed(scope: SearchScope) -> bool:
    """Quick navigation has no filters; supplement only an otherwise unscoped title search."""
    return scope.include_archived and not any(
        (
            scope.space_keys,
            scope.space_type,
            scope.content_types,
            scope.labels,
            scope.creator_usernames,
            scope.contributor_usernames,
            scope.created_after,
            scope.created_before,
            scope.modified_after,
            scope.modified_before,
            scope.parent,
            scope.ancestor,
        )
    )


def _cursor_ids(value: Any) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list) or len(value) > QUICKNAV_LIMIT:
        raise InvalidRequest("The search cursor has an invalid title-source history.")
    if any(not isinstance(item, str) or not item.isdecimal() for item in value):
        raise InvalidRequest("The search cursor has an invalid title-source history.")
    return set(value)


def _merge_hits(
    cql_hits: list[ContentSearchHit], quicknav_hits: list[ContentSearchHit], seen_ids: set[str]
) -> list[ContentSearchHit]:
    merged: list[ContentSearchHit] = []
    by_id: dict[str, ContentSearchHit] = {}
    for hit in cql_hits:
        if hit.content_id is not None and hit.content_id in seen_ids:
            continue
        if hit.content_id is not None and hit.content_id in by_id:
            continue
        copy = hit.model_copy(deep=True)
        if copy.content_id is not None:
            by_id[copy.content_id] = copy
        merged.append(copy)
    for hit in quicknav_hits:
        if hit.content_id is None:
            continue
        existing = by_id.get(hit.content_id)
        if existing is None:
            by_id[hit.content_id] = hit
            merged.append(hit)
            continue
        if "quicknav" not in existing.sources:
            existing.sources.append("quicknav")
        if existing.url is None:
            existing.url = hit.url
        if existing.space_key is None:
            existing.space_key = hit.space_key
        if existing.space_name is None:
            existing.space_name = hit.space_name
    return merged
