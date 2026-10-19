"""Offline checks for process composition, CLI behavior and MCP tool registration."""

from __future__ import annotations

import asyncio
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from confluence_connector.__main__ import EXIT_AUTH_REQUIRED, EXIT_OK, build_parser, main
from confluence_connector.auth.store import StoredCredential
from confluence_connector.bootstrap import AppContext
from confluence_connector.domain.errors import AuthenticationRequired
from confluence_connector.domain.models import AuthStatus, CurrentUser
from confluence_connector.surfaces.mcp_main import build_server
from fakes.credentials import FakeCredentialSource
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


class McpSurfaceChecks(unittest.IsolatedAsyncioTestCase):
    async def test_server_registers_only_the_two_foundation_tools(self) -> None:
        context = AppContext(settings(), store=FakeCredentialSource())  # type: ignore[arg-type]
        server = build_server(context)
        tools = await server.list_tools()
        self.assertEqual({tool.name for tool in tools}, {"auth_status", "list_spaces"})
        await context.aclose()

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
