"""Offline checks for process composition, CLI behavior and MCP tool registration."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
from confluence_connector.domain.errors import AccountMismatch, AuthenticationRequired
from confluence_connector.domain.models import AuthStatus, CurrentUser
from confluence_connector.service.spaces import SpaceService
from confluence_connector.service.timezone_calibration import CqlTimezoneCalibration
from confluence_connector.surfaces.mcp_main import build_server
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


class SpaceServiceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_verified_status_rejects_a_different_server_account(self) -> None:
        expected_usernames: list[str] = []

        class Api:
            async def current_user(self, *, expected_username: str) -> CurrentUser:
                expected_usernames.append(expected_username)
                return CurrentUser(username="different-user")

        api = Api()
        service = SpaceService(
            settings(), FakeCredentialSource(), api, "sample-user"  # type: ignore[arg-type]
        )
        with self.assertRaisesRegex(AccountMismatch, "different account"):
            await service.check_auth_status()
        self.assertEqual(expected_usernames, ["sample-user"])


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
        for name in ("download_attachments", "export_pages"):
            annotations = tools_by_name[name].annotations
            self.assertIsNotNone(annotations)
            self.assertFalse(annotations.readOnlyHint)
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

    async def test_search_tool_runs_through_the_account_bound_mcp_service(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Synthetic search result",
                            "content": {"id": "123", "type": "page", "title": "Synthetic search result"},
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
            result = await server.call_tool("search_content", {"query": "synthetic", "limit": 1})
            self.assertIn("Synthetic search result", str(result))
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.path, "/wiki/rest/api/search")
            self.assertEqual(requests[0].url.params["includeArchivedSpaces"], "true")
        finally:
            await context.aclose()
            await client.aclose()

    async def test_get_pages_tool_reads_a_synthetic_page(self) -> None:
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
                    "title": "Synthetic page",
                    "type": "page",
                    "space": {"key": "SYNTH"},
                    "version": {"number": 2, "when": "2026-10-01T00:00:00Z"},
                    "body": {"view": {"value": "<p>Page body</p>"}, "storage": {"value": "<p>Page body</p>"}},
                },
            )

        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(respond),
        )
        context = AppContext(settings(), store=FakeCredentialSource(), http_client=client)  # type: ignore[arg-type]
        server = build_server(context)
        try:
            result = await server.call_tool("get_pages", {"pages": "55"})
            self.assertIn("Page body", str(result))
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

    async def test_export_pages_reuses_the_attachment_list_for_downloads(self) -> None:
        import zipfile
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
            if request.url.path.endswith("/child/comment"):
                return httpx.Response(200, json={"results": []})
            if request.url.path.endswith("/download/attachments/55/guide.txt"):
                return httpx.Response(200, content=b"synthetic", headers={"content-type": "text/plain"})
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
                await server.call_tool("export_pages", {"pages": "55", "include_attachments": True})
                output_files = list(Path(directory).rglob("*.zip"))
                self.assertEqual(len(output_files), 1)
                with zipfile.ZipFile(output_files[0]) as archive:
                    self.assertIn("Synthetic export (55) v2/guide.txt", archive.namelist())
                    self.assertEqual(
                        archive.read("Synthetic export (55) v2/guide.txt"), b"synthetic"
                    )
                    exported_text = archive.read("Synthetic export (55) v2.txt").decode("utf-8")
                    self.assertIn("guide.txt", exported_text)
                self.assertEqual(
                    sum(request.url.path.endswith("/child/attachment") for request in requests), 1
                )
                self.assertEqual(
                    sum(request.url.path.endswith("/download/attachments/55/guide.txt") for request in requests),
                    1,
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
