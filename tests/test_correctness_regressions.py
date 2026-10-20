"""Synthetic integration regressions for the whole-project correctness review."""

from __future__ import annotations

import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

import run_acceptance
import setup_acceptance
from acceptance_fixtures import new_fixture, root_storage
from confluence_connector.domain.errors import InvalidRequest, Upstream
from confluence_connector.domain.models import PageSummary, RemoteContentSearchPage, RemoteTrashPage, TrashPageSummary
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.remote.transport import ConfluenceTransport
from confluence_connector.service.page_text import render_page_body
from confluence_connector.service.metadata import PageMetadataService
from confluence_connector.service.search import _build_cql
from confluence_connector.domain.models import SearchScope
from confluence_connector.service.structure import PageStructure
from confluence_connector.service.timezone_calibration import calibrate_cql_timezone
from confluence_connector.service.writes import PageWrites
from fakes.credential_source import FakeCredentialSource
from fakes.settings import settings
from test_structure_service import _StructureApi, _Links, _page
from test_timezone_calibration import _REFERENCE_UTC, _page_row


class CorrectnessChecks(unittest.IsolatedAsyncioTestCase):
    def api(self, handler):
        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/", transport=httpx.MockTransport(handler)
        )
        self.addAsyncCleanup(client.aclose)
        transport = ConfluenceTransport(settings(), FakeCredentialSource(), client=client)
        api = ConfluenceApi(settings(), transport)
        return api, PageLinkResolver(settings(), transport, api)

    async def test_bad_url_is_a_domain_error_and_trash_display_uses_content_collection(self):
        requests = []
        def respond(request):
            requests.append(request)
            if request.url.path.endswith("/42"):
                return httpx.Response(200, json={"id": "42", "title": "Old Plan", "type": "page"})
            return httpx.Response(200, json={"results": [{"id": "42", "title": "Old Plan"}]})
        api, links = self.api(respond)
        with self.assertRaises(InvalidRequest):
            await links.resolve("https://[broken", expected_username="sample-user")
        resolved = await links.resolve(
            "https://confluence.example.test/wiki/display/LAB/Old+Plan",
            expected_username="sample-user", status="trashed",
        )
        self.assertEqual(resolved.page_id, "42")
        self.assertEqual(requests[0].url.path, "/wiki/rest/api/content")
        self.assertEqual(dict(requests[0].url.params), {
            "type": "page", "spaceKey": "LAB", "title": "Old Plan", "status": "trashed", "limit": "2"
        })
        api.list_page_versions = AsyncMock(return_value=([], False))
        result = await PageMetadataService(api, links, "sample-user").get_page_metadata(["42", "https://[broken"])
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("InvalidRequest", result.failures[0].error)

    async def test_spaces_follow_short_pages_without_following_next_urls(self):
        starts = []
        def respond(request):
            status = request.url.params["status"]
            start = int(request.url.params["start"])
            starts.append((status, start))
            if status == "archived":
                return httpx.Response(200, json={"results": [], "start": 0, "size": 0, "_links": {}})
            return httpx.Response(200, json={
                "results": [{"key": f"S{start}", "name": "Synthetic", "type": "global"}],
                "start": start, "size": 1,
                "_links": {"next": "https://foreign.example.test/?start=500"} if start == 0 else {},
            })
        api, _ = self.api(respond)
        result = await api.list_spaces(expected_username="sample-user")
        self.assertEqual([s.key for s in result.spaces], ["S0", "S1"])
        self.assertEqual(starts, [("current", 0), ("current", 1), ("archived", 0)])

    async def test_labels_beyond_expansion_are_removed_by_the_public_writer(self):
        deleted = []
        starts = []
        def respond(request):
            if request.method == "DELETE":
                deleted.append(request.url.path)
                return httpx.Response(204)
            if request.url.path.endswith("/label"):
                start = int(request.url.params["start"])
                starts.append(start)
                return httpx.Response(200, json={
                    "results": [{"name": "first" if start == 0 else "old"}], "start": start, "size": 1,
                    "_links": {"next": "?start=200"} if start == 0 else {},
                })
            return httpx.Response(200, json={
                "id": "42", "title": "Synthetic", "type": "page", "status": "current",
                "metadata": {"labels": {"results": [{"name": "first"}], "_links": {"next": "?start=200"}}},
            })
        api, links = self.api(respond)
        result = await PageWrites(api, links, "sample-user", "https://confluence.example.test/wiki").set_labels("42", remove=["old"])
        self.assertEqual(result.removed, ["old"])
        self.assertEqual(starts, [0, 1])
        self.assertEqual(deleted, ["/wiki/rest/api/content/42/label/old"])

    async def test_subtree_preflight_reads_every_short_page_before_trashing(self):
        api = _StructureApi()
        api.pages["10"] = _page("10", "Parent")
        api.add_child("10", "11", "First")
        api.add_child("10", "12", "Second")
        original = api.list_child_pages
        starts = []
        async def short_pages(page_id, *, start, expected_username):
            self.assertEqual(api.trash_calls, [])
            page = await original(page_id, start=start, expected_username=expected_username)
            if page_id != "10":
                return page
            starts.append(start)
            return RemoteContentSearchPage(
                hits=page.hits[:1], start=start, size=min(1, page.size), has_more=start == 0
            )
        api.list_child_pages = short_pages
        result = await PageStructure(api, _Links(), "sample-user").delete_page("10", children="trash")
        self.assertTrue(result.complete)
        self.assertEqual(api.trash_calls, ["12", "11", "10"])
        self.assertIn(1, starts)
        self.assertEqual(api.permission_checks, ["10", "11", "12"])

    async def test_move_up_skips_concurrent_parent_or_space_changes(self):
        for changed_field in ("parent", "space"):
            with self.subTest(changed_field=changed_field):
                api = _StructureApi()
                api.pages["10"] = _page("10", "Parent")
                api.add_child("10", "11", "Child")
                original = api.get_page_metadata
                async def changed(page_id, **kwargs):
                    page = await original(page_id, **kwargs)
                    if page_id == "11":
                        if changed_field == "parent":
                            page.ancestors = [PageSummary(page_id="99", title="Other")]
                        else:
                            page.space_key = "OTHER"
                    return page
                api.get_page_metadata = changed
                result = await PageStructure(api, _Links(), "sample-user").delete_page("10", children="move_up")
                self.assertEqual([p.status for p in result.pages], ["trashed", "skipped"])
                self.assertEqual(api.parent_updates, [])
                self.assertEqual(api.move_calls, [])

    async def test_move_up_to_root_uses_top_level_and_definite_failure_is_failed(self):
        api = _StructureApi()
        api.pages["10"] = _page("10", "Parent")
        api.add_child("10", "11", "Child")
        api.fail_trash_for["10"] = Upstream("Synthetic refusal")
        result = await PageStructure(api, _Links(), "sample-user").delete_page("10", children="move_up")
        self.assertFalse(result.complete)
        self.assertEqual([p.status for p in result.pages], ["failed", "moved_up"])
        self.assertEqual(api.parent_updates, [])
        self.assertEqual(api.move_calls[0]["position"], "topLevel")

    async def test_refused_root_action_keeps_the_original_parent_in_the_result(self):
        api = _StructureApi()
        api.pages["10"] = _page("10", "Moving", parent_id="20")
        api.move_page_action = AsyncMock(side_effect=Upstream("Synthetic refusal"))
        result = await PageStructure(api, _Links(), "sample-user").move_page("10", space_key="SYNTH")
        self.assertFalse(result.moved)
        self.assertFalse(result.position_applied)
        self.assertEqual(result.parent_id, "20")
        self.assertEqual(api.parent_updates, [])

    async def test_calibration_retries_a_changed_sample_and_abandons_repeated_changes(self):
        for keep_changing in (False, True):
            with self.subTest(keep_changing=keep_changing):
                selections = 0
                version = 0
                def respond(request):
                    nonlocal selections, version
                    cql = request.url.params.get("cql", "")
                    if cql.startswith("type = page"):
                        selections += 1
                    elif "lastmodified >=" in cql and (selections == 1 or keep_changing):
                        version = selections
                    instant = _REFERENCE_UTC + timedelta(minutes=version)
                    row = _page_row()
                    row["content"]["version"]["when"] = instant.isoformat()
                    if not cql:
                        return httpx.Response(200, json=row["content"])
                    match = re.search(r"lastmodified >= '([^']+)'", cql)
                    rows = [row]
                    if match and match.group(1) > instant.strftime("%Y/%m/%d %H:%M"):
                        rows = []
                    return httpx.Response(200, json={"results": rows, "start": 0, "size": len(rows)})
                api, _ = self.api(respond)
                if keep_changing:
                    with self.assertRaisesRegex(Upstream, "sample changed"):
                        await calibrate_cql_timezone(api, "sample-user")
                else:
                    measured = await calibrate_cql_timezone(api, "sample-user")
                    self.assertEqual(measured.offset_minutes, 0)
                self.assertEqual(selections, 2)



class RendererRegressions(unittest.TestCase):
    def test_inner_punctuation_is_quoted_for_both_search_modes(self):
        for match in ("all", "any"):
            cql = _build_cql(["ci-cd", "alpha"], match, "all", SearchScope())
            self.assertIn('"ci', cql)
            self.assertIn('cd"', cql)
            self.assertIn("alpha", cql)

    def test_attachment_identity_respects_origin_and_source_owner(self):
        from confluence_connector.domain.models import PageAttachment
        view = (
            '<a href="https://other.example.test/download/attachments/100/report.pdf">report.pdf</a>'
            '<a href="/download/attachments/900/report.pdf" data-linked-resource-id="777">report.pdf</a>'
            '<img src="/download/attachments/900/report.pdf" data-linked-resource-filename="report.pdf" data-linked-resource-id="777" />'
            '<a href="/pages/viewpage.action" data-page-id="44">target</a>'
        )
        result = render_page_body(view, "", page_id="100", base_url="https://wiki.example.test", attachments=[PageAttachment(attachment_id="200", filename="report.pdf")])
        text = "\n\n".join(b.text for b in result.blocks)
        self.assertIn("https://other.example.test/download/attachments/100/report.pdf", text)
        files = [r for r in result.references if r.kind in {"image", "attachment"}]
        self.assertEqual({r.attachment_id for r in files}, {"777"})
        self.assertEqual({r.owner_page_id for r in files}, {"900"})
        self.assertEqual({r.filename for r in files}, {"report.pdf"})
        self.assertIn("44", {r.page_id for r in result.references})

    def test_included_images_without_an_owner_never_match_local_names(self):
        from confluence_connector.domain.models import PageAttachment
        result = render_page_body(
            '<div data-macro-name="include"><img data-linked-resource-filename="report.pdf" /></div>',
            "", page_id="100", attachments=[PageAttachment(attachment_id="200", filename="report.pdf")],
        )
        image = next(r for r in result.references if r.kind == "image")
        self.assertEqual(image.filename, "report.pdf")
        self.assertIsNone(image.owner_page_id)
        self.assertIsNone(image.attachment_id)

    def test_code_fence_text_cannot_split_parsed_code_into_budget_blocks(self):
        result = render_page_body('<p>Before</p><pre>one\n```\n\ntwo\n~~~\n\nthree</pre><p>After</p>', "", page_id="100", attachments=[])
        self.assertEqual(len(result.blocks), 3)
        self.assertIn("one\n```\n\ntwo\n~~~\n\nthree", result.blocks[1].text)
        self.assertTrue(result.blocks[1].text.startswith("````\n"))
        self.assertTrue(result.blocks[1].text.endswith("\n````"))

    def test_jira_without_storage_keeps_rendered_text(self):
        result = render_page_body('<span data-macro-name="jira">SYN-123: synthetic issue</span>', "", page_id="100", attachments=[], macros_from_storage=False)
        self.assertIn("SYN-123: synthetic issue", result.blocks[0].text)

