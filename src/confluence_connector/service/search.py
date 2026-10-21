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
)
from confluence_connector.domain.cql import cql_literal
from confluence_connector.domain.models import (
    CompactContentSearchHit,
    ContentSearchHit,
    ContentSearchResult,
    RemoteContentSearchPage,
    SearchDetail,
    SearchFields,
    SearchMatch,
    SearchOrderBy,
    SearchQuery,
    SearchScope,
)
from confluence_connector.domain.validation import page_reference, space_key
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.service.cql_dates import (
    cql_date_literal,
    date_range_is_inverted,
    normalize_scope_time,
)
from confluence_connector.service import cursors

CURSOR_KIND = "search_content"
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_OFFSET = 1_000_000
MAX_QUERY_CHARS = 1_000
MAX_SCOPE_VALUES = 50
MAX_SCOPE_VALUE_CHARS = 255
QUICKNAV_LIMIT = 20
DEFAULT_RESPONSE_MAX_CHARS = 40_000
COMMON_ENGLISH_STOPWORDS = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such that the their "
    "then there these they this to was will with".split()
)
ORDER_BY_CQL: dict[SearchOrderBy, str | None] = {
    "relevance": None,
    "modified_desc": "lastmodified DESC",
    "modified_asc": "lastmodified ASC",
    "created_desc": "created DESC",
}


class ContentSearch:
    """Public search entry point; validates tool input before constructing CQL or making requests."""

    def __init__(
        self,
        api: ConfluenceApi,
        links: PageLinkResolver,
        username: str,
        cql_timezone: str | None = None,
        response_max_chars: int = DEFAULT_RESPONSE_MAX_CHARS,
    ) -> None:
        self._api = api
        self._links = links
        self._username = username
        self._cql_timezone = cql_timezone
        self._response_max_chars = response_max_chars

    async def search_content(
        self,
        query: str | None = None,
        *,
        scope: SearchScope | dict[str, Any] | None = None,
        match: SearchMatch | None = None,
        fields: SearchFields | None = None,
        order_by: SearchOrderBy | None = None,
        detail: SearchDetail | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContentSearchResult:
        """Return one CQL page, optionally supplementing title searches with quick-navigation hits.

        Entry point: validates query, scope, modes, limit and cursor. A cursor carries the complete
        normalized request and must be passed alone. A missing query is allowed only with a
        narrowing scope; it defaults to pages ordered by modification time. Internal query builders
        trust this contract.
        """
        if cursor is not None:
            if any(value is not None for value in (query, scope, match, fields, order_by, detail, limit)):
                raise InvalidRequest("Pass only the cursor; it already contains the search and scope.")
            query, match, fields, order_by, detail, normalized_scope, limit, start, seen_ids = self._resume(cursor)
        else:
            match = _search_match(match)
            fields = _search_fields(fields)
            detail = _search_detail(detail)
            limit = _limit(limit)
            normalized_scope = await self._validate_scope(scope)
            has_scope_filter = _has_queryless_scope_filter(normalized_scope)
            if query is None:
                if not has_scope_filter:
                    raise InvalidRequest(
                        "Pass searchable query text, or omit query and provide a narrowing scope "
                        "filter such as contributor_usernames, creator_usernames, dates, spaces, "
                        "labels, parent or ancestor."
                    )
                if normalized_scope.content_types is None:
                    normalized_scope.content_types = ["page"]
                terms = []
            else:
                query, terms = _normalize_query(query)
            order_by = _search_order_by(order_by, has_query=bool(terms))
            start, seen_ids = 0, set()
        terms = _search_terms(query) if query is not None else []
        query_terms = _title_terms(terms) if fields == "title" else terms
        cql = _build_cql(query_terms, match, fields, normalized_scope, self._cql_timezone, order_by)

        quicknav_hits: list[ContentSearchHit] = []
        queries_sent = [SearchQuery(source="cql", value=cql)]
        notes: list[str] = []
        quicknav_allowed = _quicknav_allowed(normalized_scope)
        if query_terms and fields == "title" and start == 0 and quicknav_allowed:
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
        fetch_limit = limit
        page = await self._fetch_page(cql, normalized_scope, start, fetch_limit)
        quicknav_was_omitted = False
        while True:
            result = _search_result(
                page=page,
                page_limit=fetch_limit,
                query=query,
                match=match,
                fields=fields,
                order_by=order_by,
                detail=detail,
                scope=normalized_scope,
                limit=limit,
                seen_ids=seen_ids,
                quicknav_hits=[] if quicknav_was_omitted else quicknav_hits,
                cql=cql,
                queries_sent=queries_sent,
                notes=notes,
                start=start,
                quicknav_allowed=quicknav_allowed,
            )
            if _serialized_size(result) <= self._response_max_chars:
                return result
            if quicknav_hits and not quicknav_was_omitted:
                quicknav_was_omitted = True
                notes.append(
                    "Supplementary quick-navigation title matches were omitted to fit the response-size "
                    "budget; CQL title matching may not find every punctuation-heavy title."
                )
                continue
            if fetch_limit == 1:
                raise InvalidRequest(
                    "One search result exceeds the configured response-size budget; retry with "
                    "detail='compact' or increase search_response_max_chars."
                )
            fetch_limit = max(1, fetch_limit // 2)
            page = await self._fetch_page(cql, normalized_scope, start, fetch_limit)

    async def _fetch_page(
        self, cql: str, scope: SearchScope, start: int, limit: int
    ) -> RemoteContentSearchPage:
        """Read a CQL page using only validated scope and bounded service pagination."""
        return await self._api.search_content_page(
            cql,
            start=start,
            limit=limit,
            include_archived=scope.include_archived,
            expected_username=self._username,
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
        _check_range("created", scope.created_after, scope.created_before, self._cql_timezone)
        _check_range("modified", scope.modified_after, scope.modified_before, self._cql_timezone)
        if scope.parent is not None:
            reference = page_reference(scope.parent, field="scope.parent")
            resolved = await self._links.resolve(reference, expected_username=self._username)
            if not _is_page_id(resolved.page_id):
                raise InvalidRequest("Confluence returned an invalid page ID for scope.parent.")
            scope.parent = resolved.page_id
        if scope.ancestor is not None:
            reference = page_reference(scope.ancestor, field="scope.ancestor")
            resolved = await self._links.resolve(reference, expected_username=self._username)
            if not _is_page_id(resolved.page_id):
                raise InvalidRequest("Confluence returned an invalid page ID for scope.ancestor.")
            scope.ancestor = resolved.page_id
        return scope

    def _resume(
        self, cursor: str
    ) -> tuple[str | None, SearchMatch, SearchFields, SearchOrderBy, SearchDetail, SearchScope, int, int, set[str]]:
        """Decode and revalidate a cursor; cursors are opaque encoding, not signed authorization."""
        state = cursors.decode(cursor, CURSOR_KIND)
        try:
            raw_query = state.get("query")
            query, terms = (None, []) if raw_query is None else _normalize_query(raw_query)
            match = _search_match(state.get("match"))
            fields = _search_fields(state.get("fields"))
            order_by = _search_order_by(state.get("order_by"), has_query=bool(terms))
            detail = _search_detail(state.get("detail"))
            scope = SearchScope.model_validate(state.get("scope"))
        except ValidationError:
            raise InvalidRequest("The search cursor contains an invalid scope.") from None
        limit = _limit(state.get("limit"))
        start = _offset(state.get("start"))
        seen_ids = _cursor_ids(state.get("seen_quicknav_ids"))
        if not terms and not _has_queryless_scope_filter(scope):
            raise InvalidRequest("The search cursor contains neither query text nor a narrowing scope.")
        scope = _validate_resumed_scope(scope, self._cql_timezone)
        return query, match, fields, order_by, detail, scope, limit, start, seen_ids


def _normalize_query(value: Any) -> tuple[str, list[str]]:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_QUERY_CHARS:
        raise InvalidRequest(f"query must contain 1 to {MAX_QUERY_CHARS} characters.")
    if any(ord(char) < 32 for char in value):
        raise InvalidRequest("query cannot contain control characters.")
    terms = _search_terms(value)
    if not terms:
        raise InvalidRequest(
            "Wildcards and punctuation-only queries are not supported. To list content without "
            "keywords, omit query and provide a narrowing scope filter."
        )
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


def _search_order_by(value: Any, *, has_query: bool) -> SearchOrderBy:
    if value is None:
        return "relevance" if has_query else "modified_desc"
    if not isinstance(value, str) or value not in ORDER_BY_CQL:
        raise InvalidRequest("order_by must be relevance, modified_desc, modified_asc or created_desc.")
    if value == "relevance" and not has_query:
        raise InvalidRequest("order_by='relevance' requires a text query.")
    return value


def _search_detail(value: Any) -> SearchDetail:
    if value is None:
        return "full"
    if not isinstance(value, str) or value not in {"compact", "full"}:
        raise InvalidRequest("detail must be compact or full.")
    return value


def _has_queryless_scope_filter(scope: SearchScope) -> bool:
    """Recognize only explicit narrowing filters that can safely replace text search."""
    return any(
        (
            scope.contributor_usernames,
            scope.creator_usernames,
            scope.modified_after,
            scope.modified_before,
            scope.created_after,
            scope.created_before,
            scope.space_keys,
            scope.labels,
            scope.parent,
            scope.ancestor,
        )
    )


def _search_result(
    *,
    page: RemoteContentSearchPage,
    page_limit: int,
    query: str | None,
    match: SearchMatch,
    fields: SearchFields,
    order_by: SearchOrderBy,
    detail: SearchDetail,
    scope: SearchScope,
    limit: int,
    seen_ids: set[str],
    quicknav_hits: list[ContentSearchHit],
    cql: str,
    queries_sent: list[SearchQuery],
    notes: list[str],
    start: int,
    quicknav_allowed: bool,
) -> ContentSearchResult:
    full_hits = _merge_hits(page.hits, quicknav_hits, seen_ids)[:limit]
    next_seen_ids = set(seen_ids)
    next_seen_ids.update(
        hit.content_id
        for hit in full_hits
        if hit.content_id is not None and "quicknav" in hit.sources
    )
    if detail == "compact":
        hits = [
            CompactContentSearchHit(
                content_id=hit.content_id,
                content_type=hit.content_type,
                title=hit.title,
                space_key=hit.space_key,
                url=hit.url,
                modified_at=hit.modified_at,
                last_modified_by=hit.last_modified_by,
                parent_id=hit.parent_id,
            )
            for hit in full_hits
        ]
    else:
        hits = full_hits
    notes = list(notes)
    more = page.size == page_limit
    next_start = page.start + page.size
    next_cursor = None
    if more and next_start <= MAX_OFFSET:
        encoded = cursors.encode(
            CURSOR_KIND,
            query=query,
            match=match,
            fields=fields,
            order_by=order_by,
            detail=detail,
            scope=scope.model_dump(mode="json"),
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
    if page_limit < limit and more:
        notes.append("The page size was reduced to fit the response budget; continue with next_cursor.")
    if page.total_size is not None:
        notes.append("The total is Confluence's approximate count and can change between pages.")
    if query is not None and fields == "title" and quicknav_allowed:
        notes.append("The first page can include additional title matches from quick navigation.")
    elif query is not None and fields == "title" and start == 0:
        notes.append("Quick navigation was skipped so its unfiltered results cannot bypass scope filters.")
    if query is not None and _query_is_stopword_only(query):
        note_terms = ", ".join(repr(term) for term in _search_terms(query))
        notes.append(
            f"Query term(s) {note_terms} are common English stopwords and may not be indexed by this "
            "Confluence analyzer; this is a heuristic. Omit query to list by scope."
        )
    return ContentSearchResult(
        query=query,
        match=match,
        fields=fields,
        order_by=order_by,
        detail=detail,
        scope=scope,
        queries_sent=queries_sent,
        hits=hits,
        start=page.start,
        size=page.size,
        approximate_total=page.total_size,
        more_results_available=more,
        next_cursor=next_cursor,
        notes=notes,
    )


def _query_is_stopword_only(query: str) -> bool:
    """Use a small Lucene-like English list as a hint, not as a claim about instance analyzers."""
    terms = _search_terms(query)
    return bool(terms) and all(term.casefold() in COMMON_ENGLISH_STOPWORDS for term in terms)


def _serialized_size(result: ContentSearchResult) -> int:
    """Measure the JSON text returned by FastMCP before it reaches the client."""
    return len(result.model_dump_json())


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
        selected = value.strip()
        if name == "space_keys":
            selected = space_key(selected, field="scope.space_keys")
        if selected not in normalized:
            normalized.append(selected)
    return normalized


def _is_page_id(value: Any) -> bool:
    """Match the numeric, cursor-safe ID contract returned by page-reference resolution."""
    return isinstance(value, str) and value.isdecimal() and len(value) <= MAX_SCOPE_VALUE_CHARS


def _validate_resumed_scope(scope: SearchScope, cql_timezone: str | None) -> SearchScope:
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
    _check_range("created", scope.created_after, scope.created_before, cql_timezone)
    _check_range("modified", scope.modified_after, scope.modified_before, cql_timezone)
    for name in ("parent", "ancestor"):
        value = getattr(scope, name)
        if value is not None and not _is_page_id(value):
            raise InvalidRequest(f"The search cursor contains an invalid scope.{name}.")
    return scope


def _check_range(name: str, after: str | None, before: str | None, cql_timezone: str | None) -> None:
    if date_range_is_inverted(after, before, cql_timezone):
        raise InvalidRequest(f"scope.{name}_after must not be later than scope.{name}_before.")


def _build_cql(
    terms: list[str],
    match: SearchMatch,
    fields: SearchFields,
    scope: SearchScope,
    cql_timezone: str | None = None,
    order_by: SearchOrderBy = "relevance",
) -> str:
    search_field = "title" if fields == "title" else "siteSearch"
    escaped_terms = [
        '"' + _text_search_term(term) + '"' if match != "phrase" and any(_is_punctuation(c) for c in term)
        else _text_search_term(term) for term in terms
    ]
    if match == "all":
        clauses = [f"{search_field} ~ {cql_literal(term)}" for term in escaped_terms]
    elif match == "any":
        clauses = [f"{search_field} ~ {cql_literal(' '.join(escaped_terms))}"]
    else:
        phrase = '"' + " ".join(escaped_terms) + '"'
        clauses = [f"{search_field} ~ {cql_literal(phrase)}"]
    if scope.space_keys:
        clauses.append("space IN (" + ", ".join(cql_literal(key) for key in scope.space_keys) + ")")
    if scope.space_type is not None:
        clauses.append(f"space.type = {scope.space_type}")
    if scope.content_types:
        clauses.append("type IN (" + ", ".join(scope.content_types) + ")")
    if scope.labels:
        clauses.append("label IN (" + ", ".join(cql_literal(label) for label in scope.labels) + ")")
    if scope.creator_usernames:
        clauses.append(
            "creator IN (" + ", ".join(cql_literal(name) for name in scope.creator_usernames) + ")"
        )
    if scope.contributor_usernames:
        clauses.append(
            "contributor IN (" + ", ".join(cql_literal(name) for name in scope.contributor_usernames) + ")"
        )
    if scope.created_after:
        date_value = cql_date_literal("created_after", scope.created_after, "after", cql_timezone)
        clauses.append(f"created >= {cql_literal(date_value)}")
    if scope.created_before:
        date_value = cql_date_literal("created_before", scope.created_before, "before", cql_timezone)
        clauses.append(f"created <= {cql_literal(date_value)}")
    if scope.modified_after:
        date_value = cql_date_literal("modified_after", scope.modified_after, "after", cql_timezone)
        clauses.append(f"lastmodified >= {cql_literal(date_value)}")
    if scope.modified_before:
        date_value = cql_date_literal("modified_before", scope.modified_before, "before", cql_timezone)
        clauses.append(f"lastmodified <= {cql_literal(date_value)}")
    if scope.parent is not None:
        clauses.append(f"parent = {scope.parent}")
    if scope.ancestor is not None:
        clauses.append(f"ancestor = {scope.ancestor}")
    cql = " AND ".join(clauses)
    sort_clause = ORDER_BY_CQL[order_by]
    if sort_clause is not None:
        cql += f" ORDER BY {sort_clause}"
    if len(cql) > 8_000:
        raise InvalidRequest("The search query and scope produce an overly long CQL request.")
    return cql


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
