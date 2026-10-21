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
from fakes.structure import _StructureApi, _Links, _page
from fakes.calibration import _REFERENCE_UTC, _page_row


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
        original_trash = api.trash_page
        starts = []
        events: list[tuple[str, str]] = []

        async def short_pages(page_id, *, start, expected_username):
            events.append(("children", page_id))
            page = await original(page_id, start=start, expected_username=expected_username)
            if page_id != "10":
                return page
            starts.append(start)
            return RemoteContentSearchPage(
                hits=page.hits[:1], start=start, size=min(1, page.size), has_more=start == 0
            )

        async def record_trash(page_id, *, expected_username):
            events.append(("trash", page_id))
            await original_trash(page_id, expected_username=expected_username)

        api.list_child_pages = short_pages
        api.trash_page = record_trash
        result = await PageStructure(api, _Links(), "sample-user").delete_page("10", children="trash")
        self.assertTrue(result.complete)
        self.assertEqual(api.trash_calls, ["12", "11", "10"])
        self.assertIn(1, starts)
        self.assertEqual(api.permission_checks, ["10", "11", "12"])
        child_reads = [index for index, (kind, _page_id) in enumerate(events) if kind == "children"]
        trash_writes = [index for index, (kind, _page_id) in enumerate(events) if kind == "trash"]
        self.assertLess(max(child_reads), min(trash_writes))

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
                self.assertEqual(api.move_calls, [])

    async def test_move_up_to_root_appends_without_creating_a_version(self):
        api = _StructureApi()
        api.pages["10"] = _page("10", "Parent")
        api.add_child("10", "11", "Child")
        api.fail_trash_for["10"] = Upstream("Synthetic refusal")
        result = await PageStructure(api, _Links(), "sample-user").delete_page("10", children="move_up")
        self.assertFalse(result.complete)
        self.assertEqual([p.status for p in result.pages], ["failed", "moved_up"])
        self.assertEqual(api.move_calls[0]["position"], "below")
        self.assertEqual(api.move_calls[0]["target_id"], "10")
        self.assertEqual(api.pages["11"].version, 1)

    async def test_refused_root_action_keeps_the_original_parent_in_the_result(self):
        api = _StructureApi()
        api.pages["10"] = _page("10", "Moving", parent_id="20")
        api.move_page_action = AsyncMock(side_effect=Upstream("Synthetic refusal"))
        result = await PageStructure(api, _Links(), "sample-user").move_page("10", space_key="SYNTH")
        self.assertFalse(result.moved)
        self.assertFalse(result.position_applied)
        self.assertEqual(result.parent_id, "20")

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

    async def test_fixture_setup_uses_real_rich_write_validation(self):
        fixture = new_fixture("deadbeef")
        written = []
        def respond(request):
            if request.method == "POST":
                import json
                body = json.loads(request.content)
                written.append(body["body"]["storage"]["value"])
                return httpx.Response(200, json={"id": "42", "title": fixture.root_title, "type": "page", "status": "current", "space": {"key": "SYNTH"}, "version": {"number": 1}})
            return httpx.Response(200, json={"results": []})
        api, links = self.api(respond)
        services = SimpleNamespace(api=api, links=links, username="sample-user", writes=PageWrites(api, links, "sample-user", "https://confluence.example.test/wiki"))
        created = await setup_acceptance._ensure_page(
            services, fixture, title=fixture.root_title, body=root_storage(fixture),
            space_key="SYNTH", parent_id=None, position=None,
        )
        self.assertEqual(created, "42")
        self.assertEqual(len(written), 1)
        self.assertIn(fixture.marker, written[0])
        self.assertNotIn('ac:name="html"', written[0])
        from bs4 import BeautifulSoup
        macro = BeautifulSoup(written[0], "html.parser").find("ac:structured-macro", attrs={"ac:name": "view-file"})
        parameter = macro.find("ac:parameter", attrs={"ac:name": "name"})
        self.assertEqual(parameter.find("ri:attachment")["ri:filename"], fixture.text_filename)
        self.assertEqual(parameter.get_text(strip=True), "")


    async def test_fixture_refresh_uses_the_version_after_a_parent_move(self):
        fixture = new_fixture("deadbeef")
        before = SimpleNamespace(storage_body=f"<p>{fixture.marker}</p>", version=1, metadata=_page("42", "Fixture", parent_id="9"))
        after = SimpleNamespace(storage_body=before.storage_body, version=2)
        services = SimpleNamespace(
            username="sample-user",
            api=SimpleNamespace(find_page_id_by_title=AsyncMock(return_value="42"), get_page_content=AsyncMock(side_effect=[before, after])),
            structure=SimpleNamespace(move_page=AsyncMock()),
            writes=SimpleNamespace(update_page=AsyncMock()),
        )
        await setup_acceptance._ensure_page(services, fixture, title=fixture.root_title, body=root_storage(fixture), space_key="SYNTH", parent_id="10", position="last")
        self.assertEqual(services.writes.update_page.call_args.kwargs["expected_version"], 2)

    async def test_runner_records_successful_auth_and_fixture_checks(self):
        from contextlib import ExitStack
        services = SimpleNamespace(username="sample-user")
        context = SimpleNamespace(
            check_auth_status=AsyncMock(return_value=SimpleNamespace(checked=True, username="sample-user")),
            services=AsyncMock(return_value=services),
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(run_acceptance, "_locate_fixture", new=AsyncMock(return_value=SimpleNamespace(root="42"))))
            for name in ("_check_spaces", "_check_search_modes", "_check_search_filters", "_check_search_date_filters", "_check_cursor_and_user", "_check_tree_and_metadata", "_check_page_reads", "_check_inline_comment_marker", "_check_downloads_and_exports", "_check_concurrent_reads"):
                stack.enter_context(patch.object(run_acceptance, name, new=AsyncMock()))
            report = await run_acceptance._run_live(context, settings(), new_fixture("deadbeef"), "SYNTH", False)
        self.assertEqual([(r.name, r.status) for r in report.results[:2]], [("authentication", "PASS"), ("fixture", "PASS")])
        self.assertFalse(any(r.status == "FAIL" for r in report.results))

    async def test_acceptance_download_check_uses_ids_when_local_names_change(self):
        import tempfile
        import zipfile
        from acceptance_fixtures import image_png
        fixture = new_fixture("deadbeef")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            text = folder / "renamed (2).txt"
            image = folder / "renamed (2).png"
            single = folder / "single.txt"
            archive = folder / "bundle.zip"
            text.write_bytes(fixture.attachment_marker.encode())
            image.write_bytes(image_png())
            single.write_text("synthetic", encoding="utf-8")
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("root.txt", "synthetic")
                bundle.writestr(fixture.text_filename, text.read_bytes())
                bundle.writestr(fixture.image_filename, image.read_bytes())
            attachments = [SimpleNamespace(attachment_id="200", filename=fixture.text_filename), SimpleNamespace(attachment_id="201", filename=fixture.image_filename)]
            files = [SimpleNamespace(attachment_id="200", filename=text.name, path=str(text)), SimpleNamespace(attachment_id="201", filename=image.name, path=str(image))]
            services = SimpleNamespace(
                username="sample-user",
                api=SimpleNamespace(list_page_attachments=AsyncMock(return_value=attachments)),
                attachments=SimpleNamespace(download_attachments=AsyncMock(return_value=SimpleNamespace(files=files, failures=[]))),
                exports=SimpleNamespace(export_pages=AsyncMock(side_effect=[SimpleNamespace(path=str(single)), SimpleNamespace(path=str(archive))])),
            )
            await run_acceptance._check_downloads_and_exports(services, fixture, SimpleNamespace(root="10", child_one="11"))
            self.assertEqual(services.exports.export_pages.await_count, 2)

    async def test_acceptance_rejects_false_success_and_wrong_order(self):
        services = SimpleNamespace(
            username="sample-user",
            writes=SimpleNamespace(create_page=AsyncMock(return_value=SimpleNamespace(page_id="42"))),
            structure=SimpleNamespace(move_page=AsyncMock(return_value=SimpleNamespace(moved=True, parent_id="20", position_applied=False)), delete_page=AsyncMock(return_value=SimpleNamespace(complete=True, pages=[]))),
            api=SimpleNamespace(get_page_metadata=AsyncMock(return_value=_page("42", "Scratch"))),
        )
        pages = SimpleNamespace(root="10", child_one="20", personal_space="SYNTH")
        with self.assertRaisesRegex(AssertionError, "did not complete"):
            await run_acceptance._check_move_and_order(services, new_fixture("deadbeef"), pages)
        services.api = SimpleNamespace(
            get_page_metadata=AsyncMock(return_value=_page("42", "Moved", parent_id="20")),
            list_child_pages=AsyncMock(return_value=RemoteContentSearchPage(start=0, size=2, has_more=False, hits=[{"content_id": "42", "title": "Moved"}, {"content_id": "99", "title": "Other"}])),
        )
        with self.assertRaisesRegex(AssertionError, "sibling order"):
            await run_acceptance._assert_placement(services, "42", "SYNTH", "20", "last")
        services.api.get_page_metadata.return_value.space_key = "OTHER"
        with self.assertRaisesRegex(AssertionError, "destination"):
            await run_acceptance._assert_placement(services, "42", "SYNTH", "20", "first")

    async def test_acceptance_delete_restore_leaves_every_scratch_page_trashed(self):
        class Api(_StructureApi):
            async def list_child_pages(self, page_id, *, start, expected_username):
                ids = [key for key, page in self.pages.items() if page.status == "current" and page.ancestors and page.ancestors[-1].page_id == page_id]
                return self._child_page(ids, start)

            async def move_page_action(self, page_id, **kwargs):
                await super().move_page_action(page_id, **kwargs)
                parent = kwargs["target_id"] if kwargs["position"] == "append" else None
                self.pages[page_id].ancestors = [PageSummary(page_id=parent, title="Parent")] if parent else []

            async def list_trash_page(self, **kwargs):
                rows = [TrashPageSummary(page_id=key, title=page.title, space_key=page.space_key) for key, page in self.pages.items() if page.status == "trashed"]
                return RemoteTrashPage(pages=rows, start=0, size=len(rows), has_more=False)

        api = Api()
        api.pages["10"] = _page("10", "Fixture")
        api.add_child("10", "11", "Fixture child")
        async def create(title, body, **kwargs):
            page_id = str(len(api.pages) + 100)
            api.pages[page_id] = _page(page_id, title, parent_id=kwargs.get("parent_id"))
            return api.pages[page_id]
        services = SimpleNamespace(api=api, username="sample-user", writes=SimpleNamespace(create_page=create), structure=PageStructure(api, _Links(), "sample-user"))
        await run_acceptance._check_delete_and_restore(services, new_fixture("deadbeef"), SimpleNamespace(root="10", personal_space="SYNTH"))
        scratch = [page for key, page in api.pages.items() if key not in {"10", "11"}]
        self.assertEqual(len(scratch), 7)
        self.assertTrue(all(page.status == "trashed" for page in scratch))


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

