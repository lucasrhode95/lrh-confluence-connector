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
                                "version": {"when": "2026-10-01T00:00:00Z"},
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
        self.assertEqual((page.start, page.size, page.total_size), (0, 1, 400))
        self.assertEqual(page.hits[0].content_id, "123")
        self.assertEqual(page.hits[0].ancestors[0].title, "Root")
        self.assertEqual(page.hits[0].url, "https://confluence.example.test/wiki/spaces/SYNTH/pages/123/Synthetic")

    async def test_quicknav_maps_known_fields_and_rejects_foreign_link(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/wiki/rest/quicknav/1/search")
            return httpx.Response(
                200,
                json={
                    "contentNameMatches": [
                        {
                            "contentId": "123",
                            "title": "Quick title",
                            "spaceKey": "SYNTH",
                            "href": "https://elsewhere.example.test/pages/123",
                        },
                        {"contentId": "not-an-id", "title": "Ignored"},
                    ]
                },
            )

        hits = await self.make_api(handler).quicknav_title_matches(
            "Quick title", limit=5, expected_username="sample-user"
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].content_id, "123")
        self.assertEqual(hits[0].space_key, "SYNTH")
        self.assertIsNone(hits[0].url)

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
        self.assertEqual(seen[1].url.params["key"], "former-user")
        self.assertEqual(user.username, "former-user")

    async def test_space_root_route_and_version_paging_use_actual_size(self) -> None:
        starts: list[int] = []
        routes: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            routes.append(request.url.path)
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
                    "totalSize": 2,
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

    async def test_malformed_echoed_size_is_an_upstream_error(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": [{"title": "One", "content": {"id": "1"}}], "start": 0, "size": 2},
            )

        with self.assertRaisesRegex(Upstream, "inconsistent content-search size"):
            await self.make_api(handler).list_child_pages("1", start=0, expected_username="sample-user")


if __name__ == "__main__":
    unittest.main()
