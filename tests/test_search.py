"""Offline tests for literal CQL search, title-source merging and opaque cursors."""

from __future__ import annotations

import unittest

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    InvalidRequest,
    Upstream,
)
from confluence_connector.domain.models import (
    CompactContentSearchHit,
    ContentSearchHit,
    RemoteContentSearchPage,
    SearchScope,
)
from confluence_connector.service.search import ContentSearch, _build_cql, _normalize_query


class SearchChecks(unittest.IsolatedAsyncioTestCase):
    async def test_all_match_splits_whitespace_and_escapes_literal_terms(self) -> None:
        query, terms = _normalize_query("  (O'Brien),  alpha*  ")
        self.assertEqual(query, "O'Brien alpha")
        self.assertEqual(terms, ["O'Brien", "alpha"])
        cql = _build_cql(terms, "all", "all", SearchScope())
        self.assertEqual(cql, "siteSearch ~ '\"O\\'Brien\"' AND siteSearch ~ 'alpha'")

    async def test_any_and_phrase_are_single_literal_search_clauses(self) -> None:
        terms = ["one", "two"]
        self.assertEqual(_build_cql(terms, "any", "all", SearchScope()), "siteSearch ~ 'one two'")
        self.assertEqual(_build_cql(terms, "phrase", "all", SearchScope()), 'siteSearch ~ \'"one two"\'')

    async def test_queryless_cql_uses_scope_and_supported_orderings(self) -> None:
        scope = SearchScope(contributor_usernames=["sample-user"], space_keys=["SYNTH"])
        cql = _build_cql([], "all", "all", scope, order_by="modified_desc")
        self.assertEqual(
            cql,
            "space IN ('SYNTH') AND contributor IN ('sample-user') ORDER BY lastmodified DESC",
        )
        self.assertIn(
            "ORDER BY lastmodified ASC",
            _build_cql(["alpha"], "all", "all", SearchScope(), order_by="modified_asc"),
        )
        self.assertIn(
            "ORDER BY created DESC",
            _build_cql(["alpha"], "all", "all", SearchScope(), order_by="created_desc"),
        )
        self.assertNotIn(
            "ORDER BY",
            _build_cql(["alpha"], "all", "all", SearchScope(), order_by="relevance"),
        )
        text_ordered = _build_cql(
            ["alpha"], "all", "all", SearchScope(), order_by="modified_desc"
        )
        self.assertTrue(text_ordered.startswith("siteSearch ~ 'alpha' ORDER BY lastmodified DESC"))

    async def test_queryless_scope_search_defaults_to_pages_and_keeps_order_in_cursor(self) -> None:
        api = _SearchApi(pages=[_hit("1", "first"), _hit("2", "second")], page_size=1)
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        first = await service.search_content(
            scope={"contributor_usernames": ["sample-user"]},
            order_by="modified_desc",
            detail="compact",
            limit=1,
        )
        self.assertIsNone(first.query)
        self.assertEqual(first.order_by, "modified_desc")
        self.assertEqual(first.detail, "compact")
        self.assertIsInstance(first.hits[0], CompactContentSearchHit)
        self.assertEqual(
            first.hits[0].model_dump(),
            {
                "content_id": "1",
                "content_type": None,
                "title": "first",
                "space_key": None,
                "url": None,
                "modified_at": None,
                "last_modified_by": None,
                "parent_id": None,
            },
        )
        self.assertIn("type IN (page)", api.search_calls[0][0])
        self.assertIn("contributor IN ('sample-user')", api.search_calls[0][0])
        self.assertIn("ORDER BY lastmodified DESC", api.search_calls[0][0])
        second = await service.search_content(cursor=first.next_cursor)
        self.assertEqual(second.order_by, "modified_desc")
        self.assertEqual(second.detail, "compact")
        self.assertIn("ORDER BY lastmodified DESC", api.search_calls[1][0])
        self.assertEqual(api.search_calls[1][1], 1)

    async def test_queryless_without_narrowing_scope_is_rejected(self) -> None:
        service = ContentSearch(_SearchApi(), _Links(), "sample-user")  # type: ignore[arg-type]
        with self.assertRaisesRegex(InvalidRequest, "omit query and provide a narrowing scope"):
            await service.search_content()
        with self.assertRaisesRegex(InvalidRequest, "narrowing scope"):
            await service.search_content(scope={"content_types": ["page"]})

    async def test_queryless_defaults_modified_desc_and_rejects_relevance(self) -> None:
        api = _SearchApi()
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        await service.search_content(scope={"labels": ["release"]})
        self.assertIn("ORDER BY lastmodified DESC", api.search_calls[0][0])
        with self.assertRaisesRegex(InvalidRequest, "requires a text query"):
            await service.search_content(scope={"labels": ["release"]}, order_by="relevance")

    async def test_wildcard_error_and_stopword_note_offer_scope_listing(self) -> None:
        service = ContentSearch(_SearchApi(), _Links(), "sample-user")  # type: ignore[arg-type]
        with self.assertRaisesRegex(InvalidRequest, "Wildcards.*omit query.*narrowing scope"):
            await service.search_content("*")
        result = await service.search_content("the", scope={"contributor_usernames": ["sample-user"]})
        self.assertTrue(any("'the'" in note and "Omit query" in note for note in result.notes))

    async def test_compact_hundred_hits_stays_under_default_response_budget(self) -> None:
        api = _SearchApi(pages=[_hit(str(i), f"Synthetic title {i}") for i in range(100)])
        result = await ContentSearch(api, _Links(), "sample-user").search_content(
            scope={"contributor_usernames": ["sample-user"]}, limit=100, detail="compact"
        )  # type: ignore[arg-type]
        self.assertEqual(len(result.hits), 100)
        self.assertLess(len(result.model_dump_json()), 40_000)
        self.assertEqual(
            set(result.hits[0].model_dump()),
            {
                "content_id", "content_type", "title", "space_key", "url", "modified_at",
                "last_modified_by", "parent_id",
            },
        )

    async def test_oversized_full_pages_are_split_without_skipping_cql_rows(self) -> None:
        api = _SearchApi(pages=[_hit(str(i), f"title-{i}") for i in range(4)])
        for hit in api.pages:
            hit.excerpt = "x" * 1_200
        service = ContentSearch(api, _Links(), "sample-user", response_max_chars=3_000)  # type: ignore[arg-type]
        first = await service.search_content("title", limit=4)
        self.assertLess(len(first.hits), 4)
        self.assertLessEqual(len(first.model_dump_json()), 3_000)
        self.assertIsNotNone(first.next_cursor)
        self.assertTrue(any("reduced to fit" in note for note in first.notes))
        await service.search_content(cursor=first.next_cursor)
        self.assertEqual(api.search_calls[-1][1], len(first.hits))

    async def test_text_syntax_metacharacters_are_escaped_inside_terms(self) -> None:
        query, terms = _normalize_query('foo*bar x?y a"b a-b')
        self.assertEqual(query, 'foo*bar x?y a"b a-b')
        cql = _build_cql(terms, "all", "all", SearchScope())
        cql_escape = "\\" * 4
        for term in (
            f"foo{cql_escape}*bar",
            f"x{cql_escape}?y",
            f'a{cql_escape}"b',
            f"a{cql_escape}-b",
        ):
            self.assertIn(f"siteSearch ~ '\"{term}\"'", cql)

    async def test_scope_is_quoted_and_timestamps_convert_to_cql_wall_minutes(self) -> None:
        api = _SearchApi()
        service = ContentSearch(api, _Links(), "sample-user", "UTC+05:30")  # type: ignore[arg-type]
        await service.search_content(
            "roadmap",
            scope={
                "space_keys": ["ENG"],
                "creator_usernames": ["ENG' OR type = page"],
                "created_after": "2026-10-18T12:00:00-03:00",
                "include_archived": True,
            },
        )
        cql = api.search_calls[0][0]
        self.assertIn("space IN ('ENG')", cql)
        self.assertIn("creator IN ('ENG\\' OR type = page')", cql)
        self.assertIn("created >= '2026/10/18 20:30'", cql)
        self.assertTrue(api.search_calls[0][3])

    async def test_scope_rejects_invalid_space_keys_before_search(self) -> None:
        api = _SearchApi()
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]

        with self.assertRaisesRegex(InvalidRequest, "scope.space_keys"):
            await service.search_content("roadmap", scope={"space_keys": ["ENG!"]})

        self.assertEqual(api.search_calls, [])

    async def test_date_only_scope_works_without_timezone_but_timestamps_do_not(self) -> None:
        api = _SearchApi()
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        await service.search_content("roadmap", scope={"created_after": "2026-10-18"})
        self.assertIn("created >= '2026/10/18'", api.search_calls[0][0])

        with self.assertRaisesRegex(InvalidRequest, "status --check"):
            await service.search_content(
                "roadmap", scope={"created_after": "2026-10-18T12:00:00Z"}
            )
        self.assertEqual(len(api.search_calls), 1)

    async def test_mixed_date_bounds_compare_their_effective_calendar_days(self) -> None:
        api = _SearchApi(pages=[_hit("1", "first"), _hit("2", "second")], page_size=1, total_size=2)
        service = ContentSearch(api, _Links(), "sample-user", "UTC+02:00")  # type: ignore[arg-type]
        first = await service.search_content(
            "roadmap",
            limit=1,
            scope={
                "modified_after": "2026-10-19T12:00:00Z",
                "modified_before": "2026-10-19",
            },
        )
        self.assertIn("lastmodified >= '2026/10/19 14:00'", api.search_calls[0][0])
        self.assertIn("lastmodified <= '2026/10/19'", api.search_calls[0][0])
        await service.search_content(cursor=first.next_cursor)
        self.assertEqual(len(api.search_calls), 2)

        with self.assertRaisesRegex(InvalidRequest, "modified_after must not be later"):
            await service.search_content(
                "roadmap",
                scope={
                    "modified_after": "2026-10-19T23:00:00Z",
                    "modified_before": "2026-10-19",
                },
            )

    async def test_mixed_date_bounds_defer_to_timezone_error_when_unconfigured(self) -> None:
        api = _SearchApi()
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        with self.assertRaisesRegex(InvalidRequest, "status --check"):
            await service.search_content(
                "roadmap",
                scope={
                    "modified_after": "2026-10-19T12:00:00Z",
                    "modified_before": "2026-10-19",
                },
            )
        self.assertEqual(api.search_calls, [])

    async def test_cursor_keeps_utc_instants_and_reformats_them_for_each_page(self) -> None:
        api = _SearchApi(
            pages=[_hit("1", "one"), _hit("2", "two")], page_size=1, total_size=2
        )
        service = ContentSearch(api, _Links(), "sample-user", "UTC+00:00")  # type: ignore[arg-type]
        first = await service.search_content(
            "roadmap", limit=1, scope={"modified_after": "2026-10-18T12:00:00Z"}
        )
        self.assertIn("lastmodified >= '2026/10/18 12:00'", api.search_calls[0][0])

        service._cql_timezone = "UTC+02:00"
        await service.search_content(cursor=first.next_cursor)
        self.assertIn("lastmodified >= '2026/10/18 14:00'", api.search_calls[1][0])

    async def test_title_source_is_labelled_and_deduplicated(self) -> None:
        api = _SearchApi(
            pages=[
                _hit("10", "CQL title"),
                _hit("11", "Both sources"),
            ],
            quicknav=[_hit("11", "Both sources", source="quicknav"), _hit("12", "Quick title", source="quicknav")],
            page_size=2,
        )
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        result = await service.search_content("title!", fields="title", limit=10)
        self.assertEqual([hit.content_id for hit in result.hits], ["10", "11", "12"])
        self.assertEqual(result.hits[1].sources, ["cql", "quicknav"])
        self.assertEqual(result.hits[2].sources, ["quicknav"])
        self.assertEqual([query.source for query in result.queries_sent], ["cql", "quicknav"])
        self.assertEqual(api.quicknav_calls[0][0], "title")

    async def test_quicknav_does_not_exceed_the_page_limit_or_hide_unreturned_hits(self) -> None:
        api = _SearchApi(
            pages=[_hit(str(index), f"CQL {index}") for index in range(1, 12)],
            quicknav=[
                _hit("1", "CQL 1", source="quicknav"),
                _hit("11", "Quick title 11", source="quicknav"),
                _hit("12", "Quick title 12", source="quicknav"),
            ],
            page_size=10,
            total_size=11,
        )
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]

        first = await service.search_content("title", fields="title", limit=10)
        self.assertEqual(len(first.hits), 10)
        self.assertEqual([hit.content_id for hit in first.hits], [str(i) for i in range(1, 11)])
        self.assertIn("quicknav", first.hits[0].sources)

        second = await service.search_content(cursor=first.next_cursor)
        self.assertEqual([hit.content_id for hit in second.hits], ["11"])

    async def test_quicknav_failure_keeps_cql_results(self) -> None:
        api = _SearchApi(pages=[_hit("10", "CQL title")], quicknav_error=True)
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        result = await service.search_content("title", fields="title")
        self.assertEqual([hit.content_id for hit in result.hits], ["10"])
        self.assertEqual(len(result.notes), 2)
        self.assertIn("quick navigation was unavailable", result.notes[0].lower())

    async def test_quicknav_is_skipped_when_filters_cannot_be_applied_to_it(self) -> None:
        api = _SearchApi(pages=[_hit("10", "Scoped title")], quicknav=[_hit("99", "Other space")])
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        result = await service.search_content(
            "title", fields="title", scope={"space_keys": ["ENG"]}
        )
        self.assertEqual(api.quicknav_calls, [])
        self.assertEqual([query.source for query in result.queries_sent], ["cql"])
        self.assertTrue(any("skipped" in note for note in result.notes))

    async def test_quicknav_does_not_swallow_account_changes(self) -> None:
        for error in (
            AccountMismatch("Synthetic account switch"),
            AuthenticationRequired("Synthetic sign-out"),
        ):
            with self.subTest(error=type(error).__name__):
                api = _SearchApi(quicknav_error=error)
                service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
                with self.assertRaises(type(error)):
                    await service.search_content("title", fields="title")

    async def test_cursor_advances_by_actual_size_and_carries_request(self) -> None:
        api = _SearchApi(
            pages=[_hit("1", "one"), _hit("2", "two"), _hit("3", "three"), _hit("4", "four")],
            page_size=2,
            total_size=4,
        )
        service = ContentSearch(api, _Links(), "sample-user")  # type: ignore[arg-type]
        first = await service.search_content("Alpha beta", limit=2, match="any")
        self.assertEqual((first.start, first.size), (0, 2))
        self.assertIsNotNone(first.next_cursor)
        second = await service.search_content(cursor=first.next_cursor)
        self.assertEqual((second.start, second.size), (2, 2))
        self.assertEqual(second.query, "Alpha beta")
        self.assertEqual(second.match, "any")
        self.assertEqual([call[1] for call in api.search_calls], [0, 2])

    async def test_approximate_total_does_not_override_short_actual_page(self) -> None:
        api = _SearchApi(pages=[_hit("1", "one")], page_size=1, total_size=100)
        result = await ContentSearch(api, _Links(), "sample-user").search_content(
            "one", limit=10
        )  # type: ignore[arg-type]
        self.assertFalse(result.more_results_available)
        self.assertIsNone(result.next_cursor)

    async def test_cursor_must_be_the_only_search_argument(self) -> None:
        service = ContentSearch(_SearchApi(), _Links(), "sample-user")  # type: ignore[arg-type]
        with self.assertRaisesRegex(InvalidRequest, "Pass only the cursor"):
            await service.search_content("a second query", cursor="eyJraW5kIjoic2VhcmNoX2NvbnRlbnQifQ")

    async def test_invalid_modes_and_empty_punctuation_are_rejected(self) -> None:
        service = ContentSearch(_SearchApi(), _Links(), "sample-user")  # type: ignore[arg-type]
        for kwargs in (
            {"query": "!!!"},
            {"query": "alpha", "scope": []},
            {"query": "alpha", "match": []},
            {"query": "alpha", "fields": "body"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidRequest):
                await service.search_content(**kwargs)  # type: ignore[arg-type]

    async def test_parent_reference_is_resolved_before_cql(self) -> None:
        api = _SearchApi()
        links = _Links(page_id="55")
        await ContentSearch(api, links, "sample-user").search_content(
            "alpha", scope={"parent": "  55  "}
        )  # type: ignore[arg-type]
        self.assertEqual(links.references, ["55"])
        self.assertIn("parent = 55", api.search_calls[0][0])

    async def test_parent_and_ancestor_references_are_bounded_before_resolution(self) -> None:
        links = _Links()
        service = ContentSearch(_SearchApi(), links, "sample-user")  # type: ignore[arg-type]
        for field, reference in (("parent", "x" * 2_049), ("ancestor", "bad\x00reference")):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(InvalidRequest, "page ID or supported page URL"),
            ):
                await service.search_content("alpha", scope={field: reference})  # type: ignore[dict-item]
        self.assertEqual(links.references, [])

    async def test_resolved_parent_id_must_fit_the_cursor_contract(self) -> None:
        api = _SearchApi()
        links = _Links(page_id="7" * 256)
        with self.assertRaisesRegex(InvalidRequest, "invalid page ID"):
            await ContentSearch(api, links, "sample-user").search_content(
                "alpha", scope={"parent": "55"}
            )  # type: ignore[arg-type]
        self.assertEqual(api.search_calls, [])


class _Links:
    def __init__(self, page_id: str = "55") -> None:
        self.page_id = page_id
        self.references: list[str] = []

    async def resolve(self, reference: str, *, expected_username: str):
        self.references.append(reference)
        return type("Resolved", (), {"page_id": self.page_id})()


class _SearchApi:
    def __init__(
        self,
        *,
        pages: list[ContentSearchHit] | None = None,
        quicknav: list[ContentSearchHit] | None = None,
        quicknav_error: bool | Exception = False,
        page_size: int | None = None,
        total_size: int | None = None,
    ) -> None:
        self.pages = pages or []
        self.quicknav = quicknav or []
        self.quicknav_error = quicknav_error
        self.page_size = page_size
        self.total_size = total_size
        self.search_calls: list[tuple[str, int, int, bool]] = []
        self.quicknav_calls: list[tuple[str, int]] = []

    async def search_content_page(
        self,
        cql: str,
        *,
        start: int,
        limit: int,
        include_archived: bool,
        expected_username: str,
    ) -> RemoteContentSearchPage:
        self.search_calls.append((cql, start, limit, include_archived))
        size = self.page_size if self.page_size is not None else min(limit, len(self.pages))
        hits = self.pages[start : start + size]
        return RemoteContentSearchPage(
            hits=hits,
            start=start,
            size=len(hits),
            total_size=self.total_size,
        )

    async def quicknav_title_matches(
        self, query: str, *, limit: int, expected_username: str
    ) -> list[ContentSearchHit]:
        self.quicknav_calls.append((query, limit))
        if isinstance(self.quicknav_error, Exception):
            raise self.quicknav_error
        if self.quicknav_error:
            raise Upstream("Synthetic quick-navigation outage")
        return self.quicknav[:limit]


def _hit(content_id: str, title: str, *, source: str = "cql") -> ContentSearchHit:
    return ContentSearchHit(content_id=content_id, title=title, sources=[source])


if __name__ == "__main__":
    unittest.main()
