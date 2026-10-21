"""Offline checks for explicit fixture setup and its synthetic storage bodies."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))

import run_acceptance
import setup_acceptance
from acceptance_fixtures import attachment_text, new_fixture, root_storage
from confluence_connector.auth.store import StoredCredential
from fakes.settings import settings


class AcceptanceFixtureChecks(unittest.TestCase):
    def test_run_id_builds_synthetic_tree_names_and_search_markers(self) -> None:
        fixture = new_fixture("deadbeef")

        self.assertIn("[deadbeef]", fixture.root_title)
        self.assertIn("!", fixture.root_title)
        self.assertEqual(fixture.marker, "orchidbeacondeadbeef")
        self.assertEqual(fixture.code_identifier, "beacon_x.y_deadbeef")
        self.assertIn(b"cedarattachmentdeadbeef", attachment_text(fixture))

    def test_invalid_run_id_is_rejected(self) -> None:
        for value in ("ABCDEF12", "short", "00000000!", "x" * 17):
            with self.subTest(value=value), self.assertRaises(ValueError):
                new_fixture(value)


class AcceptanceSetupCliChecks(unittest.TestCase):
    def test_preview_does_not_build_context_or_make_requests(self) -> None:
        config_value = settings()
        config_value["acceptance"] = {"personal_space_key": "SYNTH", "cross_space_key": None}
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["setup_acceptance.py", "--config", "private.json", "--run-id", "deadbeef"]),
            patch.object(setup_acceptance.config, "load_config", return_value=config_value),
            patch.object(setup_acceptance, "AppContext", side_effect=AssertionError("must stay offline")),
            redirect_stdout(output),
        ):
            self.assertEqual(setup_acceptance.main(), 0)
        self.assertIn("No network requests or writes were made", output.getvalue())

    def test_apply_without_personal_space_skips_before_context_creation(self) -> None:
        config_value = settings()
        config_value["acceptance"] = {"personal_space_key": None, "cross_space_key": "SYNTH"}
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["setup_acceptance.py", "--config", "private.json", "--run-id", "deadbeef", "--apply"],
            ),
            patch.object(setup_acceptance.config, "load_config", return_value=config_value),
            patch.object(setup_acceptance, "AppContext", side_effect=AssertionError("must stay offline")),
            redirect_stdout(output),
        ):
            self.assertEqual(setup_acceptance.main(), 0)
        self.assertIn("SKIP:", output.getvalue())

    def test_cross_space_setting_must_differ_before_any_live_setup(self) -> None:
        config_value = settings()
        config_value["acceptance"] = {
            "personal_space_key": "SYNTH",
            "cross_space_key": "SYNTH",
        }
        with (
            patch.object(sys, "argv", ["setup_acceptance.py", "--config", "private.json", "--apply"]),
            patch.object(setup_acceptance.config, "load_config", return_value=config_value),
            patch.object(setup_acceptance, "AppContext", side_effect=AssertionError("must stay offline")),
            self.assertRaisesRegex(SystemExit, "must differ"),
        ):
            setup_acceptance.main()

    def test_upload_retries_only_safe_statuses_and_honors_retry_after(self) -> None:
        calls: list[str] = []
        real_async_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers["Authorization"])
            if len(calls) == 1:
                return httpx.Response(503, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"results": []})

        class Store:
            def load(self) -> StoredCredential:
                return StoredCredential(username="sample-user", token="synthetic-pat")

        def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return real_async_client(
                *args,
                **kwargs,
                transport=httpx.MockTransport(handler),
            )

        with (
            patch.object(setup_acceptance.httpx, "AsyncClient", side_effect=client_factory),
            patch.object(setup_acceptance.asyncio, "sleep", new_callable=AsyncMock),
        ):
            import asyncio

            asyncio.run(
                setup_acceptance._upload_file(
                    Store(),
                    "https://confluence.example.test/wiki",
                    "rest/api/content/101/child/attachment",
                    "synthetic.txt",
                    b"synthetic",
                    "text/plain",
                    expected_username="sample-user",
                )
            )
        self.assertEqual(calls, ["Bearer synthetic-pat", "Bearer synthetic-pat"])

    def test_upload_does_not_retry_an_uncertain_timeout(self) -> None:
        calls = 0
        real_async_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        class Store:
            def load(self) -> StoredCredential:
                return StoredCredential(username="sample-user", token="synthetic-pat")

        def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return real_async_client(
                *args,
                **kwargs,
                transport=httpx.MockTransport(handler),
            )

        with patch.object(setup_acceptance.httpx, "AsyncClient", side_effect=client_factory):
            import asyncio

            with self.assertRaisesRegex(SystemExit, "outcome is uncertain"):
                asyncio.run(
                    setup_acceptance._upload_file(
                        Store(),
                        "https://confluence.example.test/wiki",
                        "rest/api/content/101/child/attachment",
                        "synthetic.txt",
                        b"synthetic",
                        "text/plain",
                        expected_username="sample-user",
                    )
                )
        self.assertEqual(calls, 1)


class AcceptanceRunnerSafetyChecks(unittest.TestCase):
    def test_preview_does_not_build_context_or_make_requests(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["run_acceptance.py", "--config", "private.json", "--run-id", "deadbeef"],
            ),
            patch.object(run_acceptance.config, "load_config", return_value=settings()),
            patch.object(run_acceptance, "AppContext", side_effect=AssertionError("must stay offline")),
            redirect_stdout(output),
        ):
            self.assertEqual(run_acceptance.main(), 0)
        self.assertIn("no network requests or writes were made", output.getvalue())

    def test_live_run_without_personal_space_skips_before_context_creation(self) -> None:
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "run_acceptance.py",
                    "--config",
                    "private.json",
                    "--run-id",
                    "deadbeef",
                    "--run-live",
                ],
            ),
            patch.object(run_acceptance.config, "load_config", return_value=settings()),
            patch.object(run_acceptance, "AppContext", side_effect=AssertionError("must stay offline")),
            redirect_stdout(output),
        ):
            self.assertEqual(run_acceptance.main(), 0)
        self.assertIn("acceptance.personal_space_key is not configured", output.getvalue())
        self.assertIn("no requests were made", output.getvalue())

    def test_report_exposes_exception_type_but_not_exception_message(self) -> None:
        async def check() -> str:
            report = run_acceptance.AcceptanceReport()

            async def fail() -> None:
                raise RuntimeError("synthetic private page body")

            await report.run("synthetic_check", fail)
            output = io.StringIO()
            with redirect_stdout(output):
                report.print()
            return output.getvalue()

        import asyncio

        output = asyncio.run(check())
        self.assertIn("FAIL synthetic_check", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("synthetic private page body", output)

    def test_live_clients_close_on_their_request_event_loop(self) -> None:
        import asyncio

        fixture = new_fixture("deadbeef")
        loop_ids: list[int] = []

        class Context:
            async def aclose(self) -> None:
                loop_ids.append(id(asyncio.get_running_loop()))

        async def setup(context: Context, *_args: object) -> dict[str, int | bool]:
            loop_ids.append(id(asyncio.get_running_loop()))
            return {"pages": 4, "attachments": 2, "comments": 2, "label": True}

        async def run_live(*_args: object) -> run_acceptance.AcceptanceReport:
            loop_ids.append(id(asyncio.get_running_loop()))
            return run_acceptance.AcceptanceReport()

        context = Context()
        with patch.object(setup_acceptance, "_setup", new=setup):
            asyncio.run(setup_acceptance._setup_and_close(context, settings(), fixture, "~sample-user"))
        self.assertEqual(loop_ids[0], loop_ids[1])

        loop_ids.clear()
        with patch.object(run_acceptance, "_run_live", new=run_live):
            asyncio.run(
                run_acceptance._run_live_and_close(context, settings(), fixture, "~sample-user", False)
            )
        self.assertEqual(loop_ids[0], loop_ids[1])

    def test_search_check_retries_briefly_for_indexing(self) -> None:
        calls = 0

        class Search:
            async def search_content(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
                nonlocal calls
                calls += 1
                hits = [] if calls == 1 else [SimpleNamespace(content_id="301")]
                return SimpleNamespace(hits=hits)

        services = SimpleNamespace(search=Search())
        with patch.object(run_acceptance.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            import asyncio

            result = asyncio.run(
                run_acceptance._search_until_hit(
                    services, "synthetic marker", "301", failure="missing", limit=25
                )
            )

        self.assertEqual(calls, 2)
        self.assertEqual(result.hits[0].content_id, "301")
        sleep.assert_awaited_once_with(run_acceptance.SEARCH_INDEX_WAIT_SECONDS)


if __name__ == "__main__":
    unittest.main()
