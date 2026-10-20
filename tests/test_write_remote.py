"""Offline checks for write route spelling, payloads and source mappings."""

from __future__ import annotations

import json
import unittest

import httpx

from confluence_connector.domain.errors import Conflict, UncertainWrite, Upstream
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
                    "_links": {"next": "/rest/api/content?start=20"},
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
        self.assertTrue(page.has_more)

    async def test_draft_listing_preserves_explicit_final_page_signal(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "301", "type": "page", "status": "draft", "title": "Draft 301"},
                        {"id": "302", "type": "page", "status": "draft", "title": "Draft 302"},
                    ],
                    "start": 10,
                    "size": 2,
                    "_links": {"self": "/rest/api/content?start=10"},
                },
            )

        page = await self.make_api(handler).list_drafts_page(
            space_key=None,
            start=10,
            limit=10,
            expected_username="sample-user",
        )

        self.assertEqual(page.size, 2)
        self.assertFalse(page.has_more)

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

    async def test_position_action_reports_application_level_failures(self) -> None:
        for payload in (
            {"validationErrors": ["Synthetic refusal"]},
            {"commandDelegate": {"actionErrors": ["Synthetic refusal"]}},
            {"errorMessage": "Synthetic refusal"},
        ):
            with self.subTest(payload=payload), self.assertRaisesRegex(Upstream, "rejected"):
                await self.make_api(lambda _: httpx.Response(200, json=payload)).position_page(
                    "700",
                    space_key="SYNTH",
                    position="above",
                    target_id="101",
                    expected_username="sample-user",
                )

    async def test_structure_routes_preflight_trash_restore_properties_and_page_listing(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if request.url.path.endswith("/property/placement") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": "property-1",
                        "key": "placement",
                        "value": {"parent_id": "100"},
                        "version": {"number": 3},
                    },
                )
            if request.url.path.endswith("/property") and request.method == "POST":
                return httpx.Response(200, json={"key": "placement", "value": {"parent_id": "100"}})
            if request.url.params.get("expand") == "operations":
                return httpx.Response(200, json={"operations": [{"operation": "delete"}]})
            if request.url.path.endswith("/space/SYNTH"):
                return httpx.Response(200, json={"permissions": []})
            if request.url.path == "/wiki/rest/api/content" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "301",
                                "type": "page",
                                "status": "trashed",
                                "title": "Synthetic trashed page",
                                "space": {"key": "SYNTH", "name": "Synthetic space"},
                                "version": {"when": "2026-10-01T00:00:00Z"},
                                "_links": {"webui": "/wiki/spaces/SYNTH/pages/301/Page"},
                            }
                        ],
                        "start": 10,
                        "size": 1,
                        "totalSize": 12,
                    },
                )
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={
                    "id": "301",
                    "type": "page",
                    "title": "Synthetic page",
                    "status": "current",
                    "space": {"key": "SYNTH"},
                    "version": {"number": 4},
                },
            )

        api = self.make_api(handler)
        self.assertTrue(await api.page_supports_delete("301", expected_username="sample-user"))
        self.assertTrue(await api.space_permissions_readable("SYNTH", expected_username="sample-user"))
        await api.update_page_parent(
            "301",
            title="Synthetic page",
            next_version=4,
            parent_id="100",
            expected_username="sample-user",
        )
        await api.set_page_property(
            "301", "placement", {"parent_id": "100"}, expected_username="sample-user"
        )
        value = await api.get_page_property("301", "placement", expected_username="sample-user")
        await api.remove_page_property("301", "placement", expected_username="sample-user")
        await api.trash_page("301", expected_username="sample-user")
        restored = await api.restore_trashed_page(
            "301",
            title="Synthetic page",
            space_key="SYNTH",
            next_version=4,
            expected_username="sample-user",
        )
        trash = await api.list_trash_page(
            space_key="SYNTH", start=10, limit=100, expected_username="sample-user"
        )

        self.assertEqual(value, {"parent_id": "100"})
        self.assertEqual(restored.status, "current")
        self.assertEqual((trash.pages[0].page_id, trash.pages[0].modified_at), ("301", "2026-10-01T00:00:00Z"))
        self.assertTrue(trash.has_more)
        parent_body = json.loads(observed[2].content)
        self.assertEqual(parent_body["ancestors"], [{"id": "100"}])
        self.assertEqual(parent_body["version"], {"number": 4})
        self.assertEqual((observed[3].method, observed[4].method), ("GET", "PUT"))
        property_body = json.loads(observed[4].content)
        self.assertEqual(property_body["version"], {"number": 4})
        self.assertEqual(observed[6].method, "DELETE")
        self.assertEqual(observed[7].method, "DELETE")
        self.assertEqual(dict(observed[7].url.params), {})
        self.assertEqual(observed[8].url.params["status"], "trashed")
        self.assertEqual(json.loads(observed[8].content)["status"], "current")
        self.assertEqual(observed[9].url.params["status"], "trashed")
        self.assertEqual(observed[9].url.params["spaceKey"], "SYNTH")

    async def test_async_move_action_polls_documented_long_task(self) -> None:
        observed: list[httpx.Request] = []
        polls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal polls
            observed.append(request)
            if request.url.path.endswith("/movepage.action"):
                return httpx.Response(200, json={"longTaskId": "task-7", "successful": False})
            polls += 1
            if polls == 1:
                return httpx.Response(200, json={"finished": False, "successful": False})
            return httpx.Response(200, json={"finished": True, "successful": True})

        await self.make_api(handler).move_page_action(
            "301",
            space_key="TARGET",
            position="append",
            target_id="100",
            asynchronous=True,
            expected_username="sample-user",
        )

        self.assertEqual(len(observed), 3)
        self.assertEqual(observed[0].url.path, "/wiki/pages/movepage.action")
        self.assertEqual(observed[0].url.params["spaceKey"], "TARGET")
        self.assertEqual(observed[0].url.params["position"], "append")
        self.assertEqual(observed[0].url.params["targetId"], "100")
        self.assertEqual(observed[0].url.params["mode"], "ASYNC")
        self.assertEqual(observed[1].url.path, "/wiki/rest/api/longtask/task-7")

    async def test_async_move_reports_a_terminal_long_task_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/movepage.action"):
                return httpx.Response(200, json={"longTaskId": "task-8"})
            return httpx.Response(200, json={"finished": True, "successful": False})

        with self.assertRaisesRegex(Conflict, "did not complete successfully"):
            await self.make_api(handler).move_page_action(
                "301",
                space_key="TARGET",
                position="topLevel",
                target_id=None,
                asynchronous=True,
                expected_username="sample-user",
            )

    async def test_placement_property_is_created_when_missing(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            if request.method == "GET":
                return httpx.Response(404, json={"message": "Synthetic property is absent"})
            return httpx.Response(200, json={"id": "property-2"})

        await self.make_api(handler).set_page_property(
            "301", "placement", {"space_key": "SYNTH"}, expected_username="sample-user"
        )

        self.assertEqual([request.method for request in observed], ["GET", "POST"])
        self.assertEqual(observed[0].url.path, "/wiki/rest/api/content/301/property/placement")
        self.assertEqual(observed[1].url.path, "/wiki/rest/api/content/301/property")
        self.assertEqual(json.loads(observed[1].content), {"key": "placement", "value": {"space_key": "SYNTH"}})

    async def test_trash_page_uses_delete_without_purge_parameter(self) -> None:
        observed: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(204)

        await self.make_api(handler).trash_page("301", expected_username="sample-user")
        self.assertEqual(observed[0].method, "DELETE")
        self.assertEqual(observed[0].url.path, "/wiki/rest/api/content/301")
        self.assertEqual(dict(observed[0].url.params), {})

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
