"""Offline checks for process composition, CLI behavior and MCP tool registration."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
import os
from pathlib import Path
from unittest.mock import patch

import httpx

from confluence_connector.__main__ import (
    EXIT_AUTH_REQUIRED,
    EXIT_OK,
    _status,
    build_parser,
    main,
)
from confluence_connector.auth.store import StoredCredential
from confluence_connector.bootstrap import AppContext
from confluence_connector.domain.errors import AuthenticationRequired
from confluence_connector.domain.models import AuthStatus
from confluence_connector.service.timezone_calibration import CqlTimezoneCalibration
from confluence_connector.surfaces.mcp_main import build_server, root_paths
from fakes.credential_source import FakeCredentialSource
from fakes.settings import settings


class AppContextChecks(unittest.IsolatedAsyncioTestCase):
    async def test_offline_auth_status_reports_credential_state(self) -> None:
        store = FakeCredentialSource(username="sample-user")
        context = AppContext(settings(), store=store)  # type: ignore[arg-type]
        status = await context.auth_status()
        self.assertTrue(status.configured)
        self.assertTrue(status.token_stored)
        self.assertEqual(status.username, "sample-user")
        self.assertFalse(status.checked)
        self.assertIsNone(context._transport)

    async def test_context_status_works_when_signed_out(self) -> None:
        store = FakeCredentialSource()
        store.credential = None
        context = AppContext(settings(), store=store)  # type: ignore[arg-type]
        status = await context.auth_status()
        self.assertFalse(status.token_stored)
        self.assertIsNone(status.username)

    async def test_services_rebuild_after_account_switch(self) -> None:
        store = FakeCredentialSource(username="sample-user-a")
        context = AppContext(settings(), store=store)  # type: ignore[arg-type]
        try:
            first = await context.services()
            store.credential = StoredCredential(username="sample-user-b", token="synthetic-pat-b")
            second = await context.services()
            self.assertEqual(first.username, "sample-user-a")
            self.assertEqual(second.username, "sample-user-b")
            self.assertIsNot(first, second)
        finally:
            await context.aclose()

    async def test_status_check_verifies_stored_username(self) -> None:
        store = FakeCredentialSource()
        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"username": "sample-user"})
            ),
        )
        context = AppContext(settings(), store=store, http_client=client)
        status = await context.check_auth_status()
        self.assertTrue(status.checked)
        self.assertEqual(status.username, "sample-user")
        await context.aclose()
        await client.aclose()

    async def test_candidate_token_can_be_verified_before_any_token_is_stored(self) -> None:
        store = FakeCredentialSource()
        store.credential = None
        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"username": "sample-user"}
                    if request.headers.get("Authorization") == "Bearer candidate-pat"
                    else {},
                )
            ),
        )
        context = AppContext(settings(), store=store, http_client=client)
        user = await context.verify_token("candidate-pat")
        self.assertEqual(user.username, "sample-user")
        self.assertIsNone(store.credential)
        await context.aclose()
        await client.aclose()


class CommandLineChecks(unittest.TestCase):
    def test_parser_supports_the_documented_commands_and_config_positions(self) -> None:
        parser = build_parser()
        commands = ("auth", "status", "mcp")
        for command in commands:
            args = parser.parse_args([command])
            self.assertEqual(args.command, command)
        self.assertTrue(parser.parse_args(["auth", "--sign-out"]).sign_out)
        self.assertTrue(parser.parse_args(["status", "--check"]).check)
        config_path = Path("synthetic-config.json")
        self.assertEqual(parser.parse_args(["--config", str(config_path), "status"]).config, config_path)
        self.assertEqual(parser.parse_args(["status", "--config", str(config_path)]).config, config_path)

    def test_sign_out_is_explicit_and_does_not_load_site_configuration(self) -> None:
        class Store:
            def sign_out(self) -> bool:
                return True

        output = io.StringIO()
        with (
            patch("confluence_connector.__main__.TokenStore", return_value=Store()),
            patch("confluence_connector.__main__.load_config") as load,
            redirect_stdout(output),
        ):
            result = main(["auth", "--sign-out"])
        self.assertEqual(result, EXIT_OK)
        self.assertIn("cannot be recovered", output.getvalue())
        self.assertIn("not revoked", output.getvalue())
        load.assert_not_called()

    def test_auth_loads_config_before_hidden_prompt(self) -> None:
        from confluence_connector.__main__ import _auth

        with (
            patch("confluence_connector.__main__.load_config", side_effect=SystemExit("Missing")),
            patch("confluence_connector.__main__.getpass.getpass") as prompt,
            self.assertRaisesRegex(SystemExit, "Missing"),
        ):
            _auth(False, None)
        prompt.assert_not_called()

    def test_offline_status_does_not_need_a_token(self) -> None:
        class Context:
            def __init__(self, *_: object, **__: object) -> None:
                pass

            async def auth_status(self) -> AuthStatus:
                return AuthStatus(
                    configured=True,
                    token_stored=False,
                    base_url="https://confluence.example.test/wiki",
                )

            async def aclose(self) -> None:
                return None

        output = io.StringIO()
        with (
            patch("confluence_connector.__main__.load_config", return_value=settings()),
            patch("confluence_connector.__main__.AppContext", Context),
            redirect_stdout(output),
        ):
            result = main(["status"])
        self.assertEqual(result, EXIT_OK)
        self.assertIn("No Confluence token is stored", output.getvalue())

    def test_status_check_uses_auth_required_exit_code(self) -> None:
        class Context:
            def __init__(self, *_: object, **__: object) -> None:
                pass

            async def check_auth_status(self) -> AuthStatus:
                raise AuthenticationRequired("No Confluence token is stored.")

            async def aclose(self) -> None:
                return None

        error = io.StringIO()
        with (
            patch("confluence_connector.__main__.load_config", return_value=settings()),
            patch("confluence_connector.__main__.AppContext", Context),
            redirect_stderr(error),
        ):
            result = main(["status", "--check"])
        self.assertEqual(result, EXIT_AUTH_REQUIRED)
        self.assertIn("confluence-connector auth", error.getvalue())

    def test_timezone_calibration_runs_only_for_status_check_and_warns_on_mismatch(self) -> None:
        from datetime import datetime, timezone

        class Context:
            instances: list[Context] = []

            def __init__(self, *_: object, **__: object) -> None:
                self.calibrated = False
                self.instances.append(self)

            async def auth_status(self) -> AuthStatus:
                return AuthStatus(
                    configured=True,
                    token_stored=True,
                    username="sample-user",
                    base_url="https://confluence.example.test/wiki",
                )

            async def check_auth_status(self) -> AuthStatus:
                return await self.auth_status()

            async def calibrate_cql_timezone(self) -> CqlTimezoneCalibration:
                self.calibrated = True
                return CqlTimezoneCalibration(
                    offset_minutes=60,
                    reference_utc=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
                )

            async def aclose(self) -> None:
                return None

        configured = settings()
        configured["cql_timezone"] = "UTC+00:00"
        output = io.StringIO()
        with patch("confluence_connector.__main__.AppContext", Context), redirect_stdout(output):
            self.assertEqual(_status(configured, check=False), EXIT_OK)
            self.assertEqual(_status(configured, check=True), EXIT_OK)
        self.assertEqual([context.calibrated for context in Context.instances], [False, True])
        self.assertIn("differs from the measured CQL offset", output.getvalue())


class McpSurfaceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_server_registers_read_file_and_write_tools(self) -> None:
        context = AppContext(settings(), store=FakeCredentialSource())  # type: ignore[arg-type]
        server = build_server(context)
        tools = await server.list_tools()
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "auth_status",
                "list_spaces",
                "search_content",
                "find_users",
                "get_page_tree",
                "get_page_metadata",
                "get_pages",
                "download_attachments",
                "upload_attachments",
                "update_attachment",
                "trash_attachments",
                "restore_attachments",
                "export_pages",
                "create_page",
                "update_page",
                "list_drafts",
                "publish_draft",
                "delete_draft",
                "set_labels",
                "move_page",
                "delete_page",
                "list_trash",
                "restore_page",
            },
        )
        tools_by_name = {tool.name: tool for tool in tools}
        self.assertIn("pages", tools_by_name["get_pages"].outputSchema["properties"])
        self.assertIn("warnings", tools_by_name["update_page"].outputSchema["properties"])
        for name in (
            "download_attachments",
            "upload_attachments",
            "update_attachment",
            "trash_attachments",
            "restore_attachments",
            "export_pages",
        ):
            annotations = tools_by_name[name].annotations
            self.assertIsNotNone(annotations)
            self.assertFalse(annotations.readOnlyHint)
            self.assertTrue(annotations.destructiveHint)
            self.assertTrue(annotations.openWorldHint)
            self.assertFalse(annotations.idempotentHint)
        search_schema = tools_by_name["search_content"].model_dump(mode="json")["inputSchema"]
        self.assertIn("Literal search text", search_schema["properties"]["query"]["description"])
        self.assertNotIn("query", search_schema.get("required", []))
        self.assertIn("order_by", search_schema["properties"])
        self.assertIn("detail", search_schema["properties"])
        self.assertIn("contributor means anyone", search_schema["properties"]["query"]["description"])
        scope_properties = search_schema["$defs"]["SearchScope"]["properties"]
        self.assertTrue(all("description" in prop for prop in scope_properties.values()))
        self.assertIn("no offset means UTC", scope_properties["modified_after"]["description"])
        get_pages_schema = tools_by_name["get_pages"].model_dump(mode="json")["inputSchema"]
        parse_scope_schema = get_pages_schema["$defs"]["PageParseScope"]["properties"]
        self.assertTrue(all("description" in prop for prop in parse_scope_schema.values()))
        attachment_schema = tools_by_name["download_attachments"].model_dump(mode="json")["inputSchema"]
        self.assertEqual(attachment_schema["properties"]["attachment_ids"]["maxItems"], 500)
        label_annotations = tools_by_name["set_labels"].annotations
        self.assertIsNotNone(label_annotations)
        self.assertTrue(label_annotations.destructiveHint)
        publish_annotations = tools_by_name["publish_draft"].annotations
        self.assertIsNotNone(publish_annotations)
        self.assertTrue(publish_annotations.destructiveHint)
        structure_annotations = tools_by_name["delete_page"].annotations
        self.assertIsNotNone(structure_annotations)
        self.assertTrue(structure_annotations.destructiveHint)
        self.assertTrue(tools_by_name["list_trash"].annotations.readOnlyHint)
        await context.aclose()

    async def test_page_writes_accept_a_body_file_and_instructions_describe_file_edits(self) -> None:
        context = AppContext(settings(), store=FakeCredentialSource())  # type: ignore[arg-type]
        server = build_server(context)
        schemas = {
            tool.name: tool.model_dump(mode="json")["inputSchema"] for tool in await server.list_tools()
        }
        update = schemas["update_page"]
        self.assertIn("body_file", update["properties"])
        self.assertEqual(update["required"], ["page"])
        self.assertIn("body_file", schemas["create_page"]["properties"])
        self.assertEqual(schemas["create_page"]["required"], ["title"])
        self.assertIn("as `body_file`", server.instructions)
        await context.aclose()

    def test_client_root_uris_map_to_local_paths_and_other_schemes_are_ignored(self) -> None:
        paths = root_paths([
            Path("C:/work/repo one").as_uri() if os.name == "nt" else Path("/work/repo one").as_uri(),
            "https://example.test/not-a-root",
        ])
        expected = Path("C:/work/repo one") if os.name == "nt" else Path("/work/repo one")
        self.assertEqual(paths, [expected])

    async def test_read_mcp_tools_round_trip_synthetic_responses(self) -> None:
        cases = (
            (
                "search_content",
                {"query": "synthetic", "limit": 1},
                "Synthetic search result",
                {"/wiki/rest/api/search"},
            ),
            (
                "get_pages",
                {"pages": "55"},
                "Page body",
                {
                    "/wiki/rest/api/content/55",
                    "/wiki/rest/api/content/55/child/attachment",
                    "/wiki/rest/api/content/55/child/comment",
                },
            ),
        )
        for tool_name, arguments, expected_text, expected_routes in cases:
            with self.subTest(tool=tool_name):
                requests: list[httpx.Request] = []

                def respond(request: httpx.Request) -> httpx.Response:
                    requests.append(request)
                    if request.url.path == "/wiki/rest/api/search":
                        return httpx.Response(
                            200,
                            json={
                                "results": [
                                    {
                                        "title": "Synthetic search result",
                                        "content": {
                                            "id": "123",
                                            "type": "page",
                                            "title": "Synthetic search result",
                                        },
                                    }
                                ],
                                "start": 0,
                                "size": 1,
                                "totalSize": 1,
                            },
                        )
                    if request.url.path.endswith("/child/attachment"):
                        return httpx.Response(200, json={"results": []})
                    if request.url.path.endswith("/child/comment"):
                        return httpx.Response(200, json={"results": []})
                    if request.url.path == "/wiki/rest/api/content/55":
                        return httpx.Response(
                            200,
                            json={
                                "id": "55",
                                "title": "Synthetic page",
                                "type": "page",
                                "space": {"key": "SYNTH"},
                                "version": {"number": 2, "when": "2026-10-01T00:00:00Z"},
                                "body": {
                                    "view": {"value": "<p>Page body</p>"},
                                    "storage": {"value": "<p>Page body</p>"},
                                },
                            },
                        )
                    self.fail(f"Unexpected route {request.url.path}")

                client = httpx.AsyncClient(
                    base_url="https://confluence.example.test/wiki/",
                    transport=httpx.MockTransport(respond),
                )
                context = AppContext(
                    settings(), store=FakeCredentialSource(), http_client=client
                )  # type: ignore[arg-type]
                server = build_server(context)
                try:
                    result = await server.call_tool(tool_name, arguments)
                    self.assertIn(expected_text, str(result))
                    self.assertEqual({request.url.path for request in requests}, expected_routes)
                    if tool_name == "search_content":
                        self.assertEqual(requests[0].url.params["includeArchivedSpaces"], "true")
                finally:
                    await context.aclose()
                    await client.aclose()

    async def test_download_attachments_tool_returns_a_local_file(self) -> None:
        from tempfile import TemporaryDirectory

        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/child/attachment"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "att-1",
                                "title": "guide.txt",
                                "mediaType": "text/plain",
                                "fileSize": 8,
                                "_links": {"download": "/download/attachments/55/guide.txt"},
                            }
                        ]
                    },
                )
            if request.url.path.endswith("/download/attachments/55/guide.txt"):
                return httpx.Response(200, content=b"synthetic", headers={"content-type": "text/plain"})
            self.fail(f"Unexpected route {request.url.path}")

        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(respond),
        )
        context = AppContext(settings(), store=FakeCredentialSource(), http_client=client)  # type: ignore[arg-type]
        server = build_server(context)
        with TemporaryDirectory() as directory, patch(
            "confluence_connector.config.data_dir", return_value=Path(directory)
        ):
            try:
                result = await server.call_tool(
                    "download_attachments",
                    {"page": "55", "attachment_ids": ["att-1"]},
                )
                self.assertIn("guide.txt", str(result))
                self.assertIn("size_bytes", str(result))
                self.assertIn("/wiki/download/attachments/55/guide.txt", str(requests[-1].url))
            finally:
                await context.aclose()
                await client.aclose()

    async def test_export_pages_tool_returns_a_local_text_file(self) -> None:
        from tempfile import TemporaryDirectory

        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/child/attachment"):
                return httpx.Response(200, json={"results": []})
            if request.url.path.endswith("/child/comment"):
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                200,
                json={
                    "id": "55",
                    "title": "Synthetic export",
                    "type": "page",
                    "space": {"key": "SYNTH", "name": "Synthetic Space"},
                    "version": {"number": 2, "when": "2026-10-01T00:00:00Z"},
                    "body": {
                        "view": {"value": "<p>Export body</p>"},
                        "storage": {"value": "<p>Export body</p>"},
                    },
                },
            )

        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(respond),
        )
        context = AppContext(settings(), store=FakeCredentialSource(), http_client=client)  # type: ignore[arg-type]
        server = build_server(context)
        with TemporaryDirectory() as directory, patch(
            "confluence_connector.config.data_dir", return_value=Path(directory)
        ):
            try:
                result = await server.call_tool("export_pages", {"pages": "55"})
                output_files = list(Path(directory).rglob("*.txt"))
                self.assertEqual(len(output_files), 1)
                content = output_files[0].read_text(encoding="utf-8")
                self.assertIn("Breadcrumb: Synthetic Space › Synthetic export", content)
                self.assertIn("Export body", content)
                self.assertIn("text/plain; charset=utf-8", str(result))
                self.assertEqual(
                    {request.url.path for request in requests},
                    {
                        "/wiki/rest/api/content/55",
                        "/wiki/rest/api/content/55/child/attachment",
                        "/wiki/rest/api/content/55/child/comment",
                    },
                )
            finally:
                await context.aclose()
                await client.aclose()

    async def test_both_foundation_tools_work_offline_against_fake_responses(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            status = request.url.params["status"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "42" if status == "current" else "43",
                            "key": "SYNTH" if status == "current" else "ARCHIVED",
                            "name": "Synthetic Space" if status == "current" else "Archived Synthetic Space",
                            "type": "global",
                            "status": status,
                        }
                    ],
                    "start": 0,
                    "size": 1,
                    "totalSize": 1,
                },
            )

        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(respond),
        )
        context = AppContext(settings(), store=FakeCredentialSource(), http_client=client)  # type: ignore[arg-type]
        server = build_server(context)
        try:
            with self.assertLogs("confluence_connector.surfaces.mcp_main", level="INFO") as captured:
                auth_result = await server.call_tool("auth_status", {})
                spaces_result = await server.call_tool("list_spaces", {})
            self.assertIn("sample-user", str(auth_result))
            self.assertIn("Synthetic Space", str(spaces_result))
            self.assertIn("Archived Synthetic Space", str(spaces_result))
            self.assertNotIn("Synthetic Space", "\n".join(captured.output))
            self.assertEqual([request.url.params["status"] for request in requests], ["current", "archived"])
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0].url.path, "/wiki/rest/api/space")
        finally:
            await context.aclose()
            await client.aclose()


if __name__ == "__main__":
    unittest.main()
