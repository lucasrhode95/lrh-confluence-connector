"""Offline checks for the task-two Confluence routes and source-field mappings."""

from __future__ import annotations

import unittest
from unittest.mock import patch
import httpx

from confluence_connector.domain.errors import Upstream
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.remote.transport import ConfluenceTransport
from confluence_connector.service.tree import PageTree
from fakes.credential_source import FakeCredentialSource
from fakes.settings import settings


class ReadRemoteChecks(unittest.IsolatedAsyncioTestCase):
    def make_api(self, handler):
        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(handler),
        )
        transport = ConfluenceTransport(settings(), FakeCredentialSource(), client=client)
        self.addAsyncCleanup(client.aclose)
        return ConfluenceApi(settings(), transport)

    async def test_attachment_and_comment_collections_follow_short_pages(self) -> None:
        starts = {"attachment": [], "comment": []}
        def handler(request):
            kind = request.url.path.rsplit("/", 1)[-1]
            start = int(request.url.params["start"])
            starts[kind].append(start)
            row = {"id": str(start + 1), "title": "Synthetic", "type": kind, "body": {"view": {"value": "<p>Comment</p>"}}}
            return httpx.Response(200, json={"results": [row], "start": start, "size": 1,
                "_links": {"next": "/wrong?start=500"} if start == 0 else {}})
        api = self.make_api(handler)
        self.assertEqual(len(await api.list_page_attachments("55", expected_username="sample-user")), 2)
        self.assertEqual(len(await api.list_page_comments("55", expected_username="sample-user")), 2)
        self.assertEqual(starts, {"attachment": [0, 1], "comment": [0, 1]})

    async def test_top_level_next_marker_continues_without_links(self) -> None:
        starts: list[int] = []
        def handler(request):
            start = int(request.url.params["start"])
            starts.append(start)
            page = {"results": [{"id": str(start + 1), "title": "Synthetic"}], "start": start, "size": 1}
            if start == 0:
                page["next"] = "?start=1"
            return httpx.Response(200, json=page)
        api = self.make_api(handler)
        self.assertEqual(len(await api.list_page_attachments("55", expected_username="sample-user")), 2)
        self.assertEqual(starts, [0, 1])

    async def test_links_without_next_contradicting_an_exact_total_fails(self) -> None:
        for rows in ([{"id": "1", "title": "Synthetic"}], []):
            api = self.make_api(lambda request, rows=rows: httpx.Response(200, json={
                "results": rows, "start": 0, "size": len(rows), "totalSize": 2, "_links": {},
            }))
            with self.assertRaisesRegex(Upstream, "contradictory"):
                await api.list_page_attachments("55", expected_username="sample-user")

    async def test_empty_collection_with_continuation_fails_instead_of_claiming_completion(self) -> None:
        api = self.make_api(lambda request: httpx.Response(200, json={"results": [], "_links": {"next": "?start=500"}}))
        for operation in (
            api.list_page_comments("55", expected_username="sample-user"),
            PageTree(api, None, "sample-user").get_page_tree(space_key="SYNTH"),
        ):
            with self.assertRaisesRegex(Upstream, "empty page with a continuation"):
                await operation

    async def test_attachment_cap_fails_explicitly_instead_of_returning_a_partial_list(self) -> None:
        api = self.make_api(lambda request: httpx.Response(200, json={
            "results": [{"id": "1", "title": "one"}, {"id": "2", "title": "two"}],
            "_links": {"next": "?start=2"},
        }))
        with patch("confluence_connector.remote.confluence.MAX_COLLECTION_ITEMS", 2):
            with self.assertRaisesRegex(Upstream, "safety cap.*incomplete"):
                await api.list_page_attachments("55", expected_username="sample-user")

    async def test_search_sends_archived_filter_and_maps_source_fields(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Synthetic title",
                            "excerpt": "Synthetic excerpt",
                            "content": {
                                "id": "123",
                                "type": "page",
                                "title": "Synthetic title",
                                "space": {"key": "SYNTH", "name": "Synthetic space"},
                                "version": {
                                    "when": "2026-10-01T00:00:00Z",
                                    "by": {"username": "sample-editor", "displayName": "Sample Editor"},
                                },
                                "ancestors": [{"id": "1", "title": "Root"}],
                                "_links": {"webui": "/wiki/spaces/SYNTH/pages/123/Synthetic"},
                            },
                        }
                    ],
                    "start": 0,
                    "size": 1,
                    "limit": 99,
                    "totalSize": 400,
                },
            )

        page = await self.make_api(handler).search_content_page(
            "siteSearch ~ 'synthetic'",
            start=0,
            limit=1,
            include_archived=True,
            expected_username="sample-user",
        )
        request = observed[0]
        self.assertEqual(request.url.path, "/wiki/rest/api/search")
        self.assertEqual(request.url.params["includeArchivedSpaces"], "true")
        self.assertEqual(request.url.params["limit"], "1")
        self.assertIn("content.version", request.url.params["expand"])
        self.assertEqual((page.start, page.size, page.total_size), (0, 1, 400))
        self.assertEqual(page.hits[0].content_id, "123")
        self.assertEqual(page.hits[0].last_modified_by.username, "sample-editor")
        self.assertEqual(page.hits[0].last_modified_by.display_name, "Sample Editor")
        self.assertEqual(page.hits[0].ancestors[0].title, "Root")
        self.assertEqual(page.hits[0].url, "https://confluence.example.test/wiki/spaces/SYNTH/pages/123/Synthetic")

    async def test_search_hits_drop_highlight_markers_and_html_escapes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"results": [
                {
                    "title": "Lab [@@@hl@@@beacon@@@endhl@@@] &amp; Notes",
                    "excerpt": "x = &#39;@@@hl@@@beacon@@@endhl@@@&#39;",
                    "content": {"id": "123", "type": "page", "title": "Lab [beacon] & Notes"},
                },
                {"title": "@@@hl@@@beacon@@@endhl@@@ &lt;user&gt;", "entityType": "user"},
            ], "start": 0, "size": 2})

        page = await self.make_api(handler).search_content_page(
            "siteSearch ~ 'beacon'", start=0, limit=2, include_archived=True, expected_username="sample-user"
        )
        self.assertEqual(page.hits[0].title, "Lab [beacon] & Notes")
        self.assertEqual(page.hits[0].excerpt, "x = 'beacon'")
        self.assertEqual(page.hits[1].title, "beacon <user>")

    async def test_search_result_without_version_author_maps_to_null(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Synthetic title",
                            "content": {
                                "id": "123",
                                "type": "page",
                                "title": "Synthetic title",
                                "version": {"when": "2026-10-01T00:00:00Z"},
                            },
                        }
                    ],
                    "start": 0,
                    "size": 1,
                },
            )

        page = await self.make_api(handler).search_content_page(
            "contributor IN ('sample-editor')",
            start=0,
            limit=1,
            include_archived=True,
            expected_username="sample-user",
        )
        self.assertIsNone(page.hits[0].last_modified_by)

    async def test_quicknav_reads_grouped_page_rows_and_rejects_foreign_link(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/wiki/rest/quicknav/1/search")
            self.assertEqual(request.url.params["type"], "page")
            self.assertNotIn("src", request.url.params)
            return httpx.Response(200, json={"contentNameMatches": [
                [
                    {"id": "123", "name": "Quick title", "className": "content-type-page", "spaceKey": "SYNTH",
                     "spaceName": "Synthetic", "href": "https://elsewhere.example.test/pages/123"},
                    {"id": "124", "name": "Local title", "className": "content-type-page",
                     "href": "/spaces/SYNTH/pages/124/Local+title"},
                ],
                [{"id": "125", "name": "notes.pdf", "className": "content-type-attachment-pdf"}],
                [{"name": "Search for 'Quick'", "className": "search-for"}],
            ]})

        hits = await self.make_api(handler).quicknav_title_matches(
            "Quick title", limit=5, expected_username="sample-user"
        )
        self.assertEqual([hit.content_id for hit in hits], ["123", "124"])
        self.assertEqual((hits[0].content_type, hits[0].space_key, hits[0].space_name), ("page", "SYNTH", "Synthetic"))
        self.assertIsNone(hits[0].url)
        self.assertEqual(hits[1].url, "https://confluence.example.test/wiki/spaces/SYNTH/pages/124/Local+title")

    async def test_quicknav_rejects_ungrouped_rows(self) -> None:
        api = self.make_api(lambda request: httpx.Response(200, json={"contentNameMatches": [{"id": "1", "name": "x"}]}))
        with self.assertRaisesRegex(Upstream, "unexpected quick-navigation response"):
            await api.quicknav_title_matches("x", limit=5, expected_username="sample-user")

    async def test_user_name_search_uses_user_cql_and_exact_lookup_key(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/user"):
                return httpx.Response(200, json={"username": "former-user", "displayName": "Former User"})
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Sample User",
                            "username": "sample-user",
                            "userKey": "sample-user",
                            "type": "user",
                        }
                    ],
                    "start": 0,
                    "size": 1,
                },
            )

        api = self.make_api(handler)
        page = await api.search_users_page("Sample User", start=0, limit=25, expected_username="sample-user")
        user = await api.get_user("former-user", expected_username="sample-user")
        self.assertIn("type = user", seen[0].url.params["cql"])
        self.assertIn("user.fullname ~ 'Sample User'", seen[0].url.params["cql"])
        self.assertEqual(page.users[0].username, "sample-user")
        self.assertEqual(seen[1].url.params["username"], "former-user")
        self.assertEqual(user.username, "former-user")

    async def test_space_root_route_and_version_paging_use_actual_size(self) -> None:
        starts: list[int] = []
        routes: list[str] = []
        params: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            routes.append(request.url.path)
            params.append(dict(request.url.params))
            if request.url.path.endswith("/content/page"):
                return httpx.Response(
                    200,
                    json={"results": [], "start": 0, "size": 0, "limit": 500, "totalSize": 0},
                )
            start = int(request.url.params["start"])
            starts.append(start)
            if start == 0:
                rows = [{"number": 1}, {"number": 2}]
                total = 3
            else:
                rows = [{"number": 3}]
                total = 3
            return httpx.Response(
                200,
                json={"results": rows, "start": start, "size": len(rows), "limit": 200, "totalSize": total},
            )

        api = self.make_api(handler)
        root_page = await api.list_space_root_pages(
            "SYNTH", start=0, expected_username="sample-user"
        )
        versions, truncated = await api.list_page_versions("55", expected_username="sample-user")
        self.assertEqual(root_page.size, 0)
        self.assertEqual(starts, [0, 2])
        self.assertEqual([version.number for version in versions], [1, 2, 3])
        self.assertFalse(truncated)
        self.assertEqual(routes[0], "/wiki/rest/api/space/SYNTH/content/page")
        self.assertEqual(params[0]["depth"], "root")

    async def test_page_tree_uses_actual_child_size_and_ignores_next_url(self) -> None:
        starts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/wiki/rest/api/content/1":
                return httpx.Response(
                    200,
                    json={"id": "1", "title": "Root", "type": "page", "space": {"key": "SYNTH"}},
                )
            start = int(request.url.params["start"])
            starts.append(start)
            page_id = "2" if start == 0 else "3"
            return httpx.Response(
                200,
                json={
                    "results": [{"id": page_id, "title": f"Child {page_id}", "type": "page"}],
                    "start": start,
                    "size": 1,
                    "limit": 500,
                    "_links": {"next": "https://elsewhere.example.test/skip"} if start == 0 else {},
                },
            )

        api = self.make_api(handler)
        resolver = PageLinkResolver(settings(), api._transport, api)
        tree = await PageTree(api, resolver, "sample-user").get_page_tree(page="1", max_depth=2)
        self.assertEqual(starts, [0, 1])
        self.assertEqual([node.page_id for node in tree.children], ["2", "3"])
        self.assertEqual(tree.total_pages, 3)

    async def test_version_cap_is_reported_as_truncated(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [{"number": number} for number in range(1, 4)],
                    "start": 0,
                    "size": 3,
                    "limit": 200,
                    "totalSize": 20,
                },
            )

        with patch("confluence_connector.remote.confluence.MAX_PAGE_VERSIONS", 2):
            versions, truncated = await self.make_api(handler).list_page_versions(
                "55", expected_username="sample-user"
            )
        self.assertEqual([version.number for version in versions], [1, 2])
        self.assertTrue(truncated)

    async def test_page_metadata_maps_history_labels_people_and_ancestors(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/wiki/rest/api/content/55")
            self.assertIn("history.contributors", request.url.params["expand"])
            return httpx.Response(
                200,
                json={
                    "id": "55",
                    "title": "Synthetic page",
                    "type": "page",
                    "status": "current",
                    "space": {"key": "SYNTH", "name": "Synthetic space"},
                    "_links": {"webui": "/wiki/spaces/SYNTH/pages/55/Synthetic"},
                    "history": {
                        "createdDate": "2026-10-01T00:00:00Z",
                        "createdBy": {"username": "sample-user", "displayName": "Sample User"},
                        "contributors": {
                            "publishers": {
                                "users": [{"userKey": "sample-user", "displayName": "Sample User"}]
                            }
                        },
                    },
                    "version": {
                        "number": 3,
                        "when": "2026-10-02T00:00:00Z",
                        "by": {"username": "sample-user", "displayName": "Sample User"},
                    },
                    "ancestors": [{"id": "1", "title": "Synthetic root"}],
                    "metadata": {"labels": {"results": [{"name": "reviewed"}]}},
                },
            )

        metadata = await self.make_api(handler).get_page_metadata("55", expected_username="sample-user")
        self.assertEqual((metadata.page_id, metadata.title, metadata.version), ("55", "Synthetic page", 3))
        self.assertEqual(metadata.created_by.username, "sample-user")
        self.assertEqual(metadata.contributors[0].user_key, "sample-user")
        self.assertEqual(metadata.ancestors[0].title, "Synthetic root")
        self.assertEqual(metadata.labels, ["reviewed"])

    async def test_page_content_requests_a_pinned_historical_version(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "55",
                    "title": "Synthetic page",
                    "type": "page",
                    "version": {"number": 4, "when": "2026-10-01T00:00:00Z"},
                    "history": {"totalVersions": 9},
                    "body": {
                        "view": {"value": "<p>Rendered body</p>"},
                        "storage": {"value": "<p>Stored body</p>"},
                    },
                },
            )

        content = await self.make_api(handler).get_page_content(
            "55", version=4, expected_username="sample-user"
        )
        self.assertEqual(observed[0].url.params["status"], "historical")
        self.assertEqual(observed[0].url.params["version"], "4")
        self.assertEqual(content.view_body, "<p>Rendered body</p>")
        self.assertEqual(content.storage_body, "<p>Stored body</p>")
        self.assertEqual(content.version, 4)
        self.assertEqual(content.total_versions, 9)

    async def test_page_content_rejects_unconfirmed_historical_version(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "55",
                    "title": "Synthetic page",
                    "type": "page",
                    "body": {"view": {"value": "Current body"}},
                },
            )

        with self.assertRaisesRegex(Upstream, "did not confirm the requested historical page version"):
            await self.make_api(handler).get_page_content(
                "55", version=4, expected_username="sample-user"
            )

    async def test_page_content_can_skip_storage_expansion(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "55",
                    "title": "Synthetic page",
                    "type": "page",
                    "version": {"number": 2, "when": "2026-10-01T00:00:00Z"},
                    "body": {"view": {"value": "<p>Rendered body</p>"}},
                },
            )

        content = await self.make_api(handler).get_page_content(
            "55", include_storage=False, expected_username="sample-user"
        )
        self.assertNotIn("body.storage", observed[0].url.params["expand"])
        self.assertEqual(content.view_body, "<p>Rendered body</p>")
        self.assertEqual(content.storage_body, "")

    async def test_page_content_rejects_missing_requested_storage_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "55",
                    "title": "Synthetic page",
                    "type": "page",
                    "version": {"number": 2, "when": "2026-10-01T00:00:00Z"},
                    "body": {"view": {"value": "<p>Rendered body</p>"}},
                },
            )

        with self.assertRaisesRegex(Upstream, "valid requested storage body"):
            await self.make_api(handler).get_page_content("55", expected_username="sample-user")

    async def test_page_children_map_attachment_identity_and_comment_source_fields(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if request.url.path.endswith("/child/attachment"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "attachment-9",
                                "title": "guide.txt",
                                "mediaType": "text/plain",
                                "fileSize": 42,
                                "_links": {"download": "/wiki/download/attachments/55/guide.txt"},
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "comment-7",
                            "type": "comment",
                            "ancestors": [{"id": "comment-6", "type": "comment"}],
                            "body": {"view": {"value": "<p>Synthetic comment</p>"}},
                            "history": {
                                "createdDate": "2026-10-01T00:00:00Z",
                                "createdBy": {"username": "sample-user", "displayName": "Sample User"},
                            },
                            "extensions": {
                                "location": "inline",
                                "resolutionStatus": "resolved",
                                "inlineProperties": {"originalSelection": "selected words"},
                            },
                        }
                    ]
                },
            )

        api = self.make_api(handler)
        attachments = await api.list_page_attachments("55", expected_username="sample-user")
        comments = await api.list_page_comments("55", expected_username="sample-user")
        self.assertEqual(observed[0].url.path, "/wiki/rest/api/content/55/child/attachment")
        self.assertEqual(observed[0].url.params["limit"], "500")
        self.assertEqual(observed[1].url.path, "/wiki/rest/api/content/55/child/comment")
        self.assertEqual(observed[1].url.params["depth"], "all")
        self.assertIn("history", observed[1].url.params["expand"])
        self.assertEqual(attachments[0].attachment_id, "attachment-9")
        self.assertEqual(
            attachments[0].download_url,
            "https://confluence.example.test/wiki/download/attachments/55/guide.txt",
        )
        self.assertEqual(comments[0].parent_comment_id, "comment-6")
        self.assertEqual(comments[0].location, "inline")
        self.assertEqual(comments[0].selected_text, "selected words")
        self.assertEqual(comments[0].author.display_name, "Sample User")  # type: ignore[union-attr]

    async def test_page_comments_reject_missing_or_malformed_requested_view_bodies(self) -> None:
        for body in ({}, {"view": {"value": None}}, {"view": {"value": 5}}):
            with self.subTest(body=body):
                def handler(_: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        200,
                        json={"results": [{"id": "comment-7", "body": body}]},
                    )

                with self.assertRaisesRegex(Upstream, "expanded view body"):
                    await self.make_api(handler).list_page_comments(
                        "55", expected_username="sample-user"
                    )

    async def test_unordered_positions_do_not_break_children_or_ancestor_metadata(self) -> None:
        def handler(request):
            row = {"id": "55", "title": "Synthetic", "type": "page", "position": -1,
                   "ancestors": [{"id": "1", "title": "Parent", "position": -1}]}
            if request.url.path.endswith("/child/page"):
                return httpx.Response(200, json={"results": [row], "size": 1, "start": 0})
            return httpx.Response(200, json=row)
        api = self.make_api(handler)
        metadata = await api.get_page_metadata("55", expected_username="sample-user")
        children = await api.list_child_pages("1", start=0, expected_username="sample-user")
        self.assertIsNone(metadata.ancestors[0].position)
        self.assertIsNone(children.hits[0].position)

    async def test_malformed_echoed_size_is_an_upstream_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": [{"title": "One", "content": {"id": "1"}}], "start": 0, "size": 2},
            )

        with self.assertRaisesRegex(Upstream, "inconsistent collection page offset or size"):
            await self.make_api(handler).list_child_pages("1", start=0, expected_username="sample-user")


if __name__ == "__main__":
    unittest.main()
