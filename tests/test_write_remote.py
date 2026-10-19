"""Offline checks for write route spelling, payloads and source mappings."""

from __future__ import annotations

import json
import unittest

import httpx

from confluence_connector.domain.errors import UncertainWrite
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.transport import ConfluenceTransport
from tests.fakes.credential_source import FakeCredentialSource
from tests.fakes.settings import settings


class WriteRemoteChecks(unittest.IsolatedAsyncioTestCase):
    def make_api(self, handler):
        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(handler),
        )
        transport = ConfluenceTransport(settings(), FakeCredentialSource(), client=client)
        self.addAsyncCleanup(client.aclose)
        return ConfluenceApi(settings(), transport)

    async def test_create_and_update_send_storage_representation_and_next_version(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={
                        "id": "700",
                        "type": "page",
                        "title": "New page",
                        "status": "current",
                        "space": {"key": "SYNTH"},
                        "version": {"number": 1},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "700",
                    "type": "page",
                    "title": "Renamed",
                    "status": "current",
                    "space": {"key": "SYNTH"},
                    "version": {"number": 2},
                },
            )

        api = self.make_api(handler)
        created = await api.create_page(
            title="New page",
            space_key="SYNTH",
            storage_body="<p>initial</p>",
            parent_id="100",
            status="current",
            expected_username="sample-user",
        )
        updated = await api.update_page(
            "700",
            title="Renamed",
            storage_body="<p>replacement</p>",
            next_version=2,
            expected_username="sample-user",
        )

        self.assertEqual((created.page_id, created.version), ("700", 1))
        self.assertEqual((updated.title, updated.version), ("Renamed", 2))
        self.assertEqual(observed[0].url.path, "/wiki/rest/api/content")
        create_body = json.loads(observed[0].content)
        self.assertEqual(create_body["ancestors"], [{"id": "100"}])
        self.assertEqual(create_body["body"]["storage"], {"value": "<p>initial</p>", "representation": "storage"})
        update_body = json.loads(observed[1].content)
        self.assertEqual(update_body["version"], {"number": 2})

    async def test_draft_listing_uses_status_scope_and_maps_source_fields(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "301",
                            "type": "page",
                            "status": "draft",
                            "title": "Synthetic draft",
                            "space": {"key": "SYNTH", "name": "Synthetic space"},
                            "version": {"when": "2026-10-01T00:00:00Z"},
                            "_links": {"webui": "/wiki/spaces/SYNTH/pages/301/Draft"},
                        }
                    ],
                    "start": 10,
                    "size": 1,
                    "totalSize": 11,
                },
            )

        page = await self.make_api(handler).list_drafts_page(
            space_key="SYNTH",
            start=10,
            limit=10,
            expected_username="sample-user",
        )

        self.assertEqual(observed[0].url.path, "/wiki/rest/api/content")
        self.assertEqual(observed[0].url.params["status"], "draft")
        self.assertEqual(observed[0].url.params["spaceKey"], "SYNTH")
        self.assertEqual(page.drafts[0].page_id, "301")
        self.assertEqual(page.drafts[0].space_name, "Synthetic space")
        self.assertEqual((page.start, page.size, page.total_size), (10, 1, 11))

    async def test_draft_content_read_uses_status_query_before_publication(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "301",
                    "type": "page",
                    "status": "draft",
                    "title": "Synthetic draft",
                    "space": {"key": "SYNTH"},
                    "version": {"number": 1},
                    "ancestors": [{"id": "100", "title": "Parent"}],
                    "body": {
                        "view": {"value": "<p>draft body</p>"},
                        "storage": {"value": "<p>draft body</p>"},
                    },
                },
            )

        page = await self.make_api(handler).get_page_content(
            "301",
            status="draft",
            expected_username="sample-user",
        )
        self.assertEqual(observed[0].url.params["status"], "draft")
        self.assertEqual(page.metadata.status, "draft")
        self.assertEqual(page.storage_body, "<p>draft body</p>")
        self.assertEqual(page.metadata.ancestors[0].page_id, "100")

    async def test_duplicate_title_search_distinguishes_no_match(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(200, json={"results": []})

        match = await self.make_api(handler).find_page_id_by_title(
            "SYNTH", "Synthetic title", expected_username="sample-user"
        )
        self.assertIsNone(match)
        self.assertIn("title = 'Synthetic title'", observed[0].url.params["cql"])

    async def test_publish_delete_and_label_routes_use_documented_status_and_payloads(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.method == "POST":
                return httpx.Response(200, json=[{"name": "reviewed"}])
            return httpx.Response(
                200,
                json={
                    "id": "301",
                    "type": "page",
                    "title": "Draft",
                    "status": "current",
                    "space": {"key": "SYNTH"},
                    "version": {"number": 1},
                },
            )

        api = self.make_api(handler)
        published = await api.publish_draft(
            "301",
            title="Draft",
            space_key="SYNTH",
            parent_id=None,
            storage_body="<p>body</p>",
            expected_username="sample-user",
        )
        await api.delete_draft("301", expected_username="sample-user")
        await api.add_page_label("301", "reviewed", expected_username="sample-user")
        await api.remove_page_label("301", "reviewed", expected_username="sample-user")

        self.assertEqual(published.status, "current")
        self.assertEqual(observed[0].method, "PUT")
        publish_body = json.loads(observed[0].content)
        self.assertEqual(publish_body["version"], {"number": 1})
        self.assertEqual((observed[1].method, observed[1].url.params["status"]), ("DELETE", "draft"))
        self.assertEqual(observed[2].url.path, "/wiki/rest/api/content/301/label")
        self.assertEqual(json.loads(observed[2].content), [{"prefix": "global", "name": "reviewed"}])
        self.assertTrue(observed[3].url.path.endswith("/label/reviewed"))

    async def test_position_request_is_isolated_to_move_action(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(200, json={"success": True})

        await self.make_api(handler).position_page(
            "700",
            space_key="SYNTH",
            position="above",
            target_id="101",
            expected_username="sample-user",
        )

        request = observed[0]
        self.assertEqual((request.method, request.url.path), ("POST", "/wiki/pages/movepage.action"))
        self.assertEqual(request.url.params["pageId"], "700")
        self.assertEqual(request.url.params["position"], "above")
        self.assertEqual(request.url.params["targetId"], "101")
        self.assertEqual(request.url.params["mode"], "LEGACY")
        self.assertEqual(request.headers["X-Atlassian-Token"], "no-check")

    async def test_success_status_with_unreadable_page_response_is_uncertain(self) -> None:
        api = self.make_api(lambda _: httpx.Response(200, text="<html>maintenance</html>"))

        with self.assertRaisesRegex(UncertainWrite, "inspect before retrying"):
            await api.create_page(
                title="New page",
                space_key="SYNTH",
                storage_body="<p>body</p>",
                parent_id=None,
                status="current",
                expected_username="sample-user",
            )


if __name__ == "__main__":
    unittest.main()
