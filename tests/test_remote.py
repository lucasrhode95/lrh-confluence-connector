"""Offline tests for Confluence HTTP policy, mappings and page URL resolution."""

from __future__ import annotations

import gzip
import unittest
from collections.abc import AsyncIterator, Callable
from io import BytesIO
from unittest.mock import AsyncMock, patch

import httpx

from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    Conflict,
    InvalidRequest,
    NotFound,
    Throttled,
    UncertainWrite,
    Upstream,
)
from confluence_connector.domain.models import PageAttachment
from confluence_connector.auth.store import StoredCredential
from confluence_connector.remote.confluence import ConfluenceApi
from confluence_connector.remote.links import PageLinkResolver
from confluence_connector.remote.transport import ConfluenceTransport
from fakes.credential_source import FakeCredentialSource
from fakes.settings import settings


class _PartialReadTimeout(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial"
        raise httpx.ReadTimeout("synthetic stream timeout", request=self._request)

    async def aclose(self) -> None:
        return None


class RemoteChecks(unittest.IsolatedAsyncioTestCase):
    def make_client(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        credentials: FakeCredentialSource | None = None,
        sleep: AsyncMock | None = None,
    ) -> tuple[httpx.AsyncClient, ConfluenceTransport]:
        client = httpx.AsyncClient(
            base_url="https://confluence.example.test/wiki/",
            transport=httpx.MockTransport(handler),
        )
        transport = ConfluenceTransport(settings(), credentials or FakeCredentialSource(), client=client, sleep=sleep or AsyncMock())
        self.addAsyncCleanup(client.aclose)
        return client, transport

    async def test_compressed_json_is_decoded_once_for_reads_and_writes(self) -> None:
        attempts = []
        def handler(request):
            attempts.append(request)
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b'{"ok":true}'))
        _, transport = self.make_client(handler)
        self.assertEqual(await transport.get_json("rest/api/content"), {"ok": True})
        self.assertEqual(await transport.send_json("POST", "rest/api/content", expected_username="sample-user"), {"ok": True})
        self.assertEqual(len(attempts), 2)
        with patch("confluence_connector.remote.transport.MAX_JSON_BYTES", 4):
            with self.assertRaisesRegex(Upstream, "larger than"):
                await transport.get_json("rest/api/content")

    async def test_multipart_upload_reuses_safe_retries_and_retains_file_bytes(self) -> None:
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(503, headers={"Retry-After": "0"}) if len(requests) == 1 else httpx.Response(200, json={})
        _, transport = self.make_client(handler)
        await transport.send_json("POST", "rest/api/content/42/child/attachment",
            files={"file": ("guide.txt", b"synthetic file", "text/plain")},
            skip_atlassian_token_check=True, expected_username="sample-user")
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertIn(b"synthetic file", request.content)
            self.assertIn("multipart/form-data", request.headers["Content-Type"])
            self.assertEqual(request.headers["X-Atlassian-Token"], "no-check")

    async def test_attachment_download_streams_binary_and_keeps_source_query(self) -> None:
        observed: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request)
            return httpx.Response(200, content=b"binary\x00attachment", headers={"content-type": "application/octet-stream"})

        _, transport = self.make_client(handler)
        api = ConfluenceApi(settings(), transport)
        attachment = PageAttachment(
            attachment_id="att-7",
            filename="guide.bin",
            download_url=(
                "https://confluence.example.test/wiki/download/attachments/55/guide.bin"
                "?version=2&api=v2"
            ),
        )
        destination = BytesIO()
        content_type, size = await api.download_attachment(
            attachment,
            destination,
            expected_username="sample-user",
        )
        self.assertEqual(destination.getvalue(), b"binary\x00attachment")
        self.assertEqual((content_type, size), ("application/octet-stream", 17))
        self.assertEqual(str(observed[0].url), "https://confluence.example.test/wiki/download/attachments/55/guide.bin?version=2&api=v2")
        self.assertEqual(observed[0].headers["Authorization"], "Bearer synthetic-pat")

    async def test_attachment_download_rejects_an_external_source_link(self) -> None:
        attempts = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, content=b"unexpected")

        _, transport = self.make_client(handler)
        api = ConfluenceApi(settings(), transport)
        attachment = PageAttachment(
            attachment_id="att-7",
            filename="guide.bin",
            download_url="https://outside.example.test/download/guide.bin",
        )
        with self.assertRaisesRegex(Upstream, "unsafe attachment download link"):
            await api.download_attachment(attachment, BytesIO(), expected_username="sample-user")
        self.assertEqual(attempts, 0)

    async def test_attachment_download_retries_read_timeout_without_partial_bytes(self) -> None:
        attempts = 0
        sleep = AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(200, stream=_PartialReadTimeout(request))
            return httpx.Response(200, content=b"complete")

        _, transport = self.make_client(handler, sleep=sleep)
        destination = BytesIO()
        content_type, size = await transport.download_to(
            "download/attachments/55/guide.bin",
            destination,
            expected_username="sample-user",
        )
        self.assertEqual(destination.getvalue(), b"complete")
        self.assertEqual((content_type, size), (None, 8))
        self.assertEqual(attempts, 2)
        sleep.assert_awaited_once()

    async def test_attachment_download_retries_status_and_honors_retry_after(self) -> None:
        attempts = 0
        sleep = AsyncMock()

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, headers={"Retry-After": "0"}, content=b"retry")
            return httpx.Response(200, content=b"complete")

        _, transport = self.make_client(handler, sleep=sleep)
        destination = BytesIO(b"discard")
        content_type, size = await transport.download_to(
            "download/attachments/55/guide.bin",
            destination,
            expected_username="sample-user",
        )
        self.assertEqual(destination.getvalue(), b"complete")
        self.assertEqual((content_type, size), (None, 8))
        self.assertEqual(attempts, 2)
        sleep.assert_awaited_once_with(0.0)

    async def test_read_retries_connection_reset_then_succeeds(self) -> None:
        attempts = 0
        sleep = AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("synthetic reset", request=request)
            return httpx.Response(200, json={"ok": True})

        _, transport = self.make_client(handler, sleep=sleep)
        payload = await transport.get_json("rest/api/user/current", expected_username="sample-user")
        self.assertTrue(payload["ok"])
        self.assertEqual(attempts, 2)
        sleep.assert_awaited_once()

    async def test_read_retries_each_documented_timeout(self) -> None:
        timeout_types = (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        )
        for timeout_type in timeout_types:
            with self.subTest(timeout=timeout_type.__name__):
                attempts = 0

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise timeout_type("synthetic timeout", request=request)
                    return httpx.Response(200, json={"ok": True})

                _, transport = self.make_client(handler)
                self.assertTrue((await transport.get_json("rest/api/user/current"))["ok"])
                self.assertEqual(attempts, 2)

    async def test_read_retries_each_documented_status(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                attempts = 0
                sleep = AsyncMock()

                def handler(_: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        return httpx.Response(status, headers={"Retry-After": "0"}, json={})
                    return httpx.Response(200, json={"ok": True})

                _, transport = self.make_client(handler, sleep=sleep)
                self.assertTrue((await transport.get_json("rest/api/user/current"))["ok"])
                self.assertEqual(attempts, 2)
                sleep.assert_awaited_once_with(0.0)

    async def test_write_timeout_is_uncertain_and_never_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("synthetic timeout", request=request)

        _, transport = self.make_client(handler)
        with self.assertRaises(UncertainWrite):
            await transport.send_json(
                "POST", "rest/api/content", body={"title": "Synthetic"}, expected_username="sample-user"
            )
        self.assertEqual(attempts, 1)

    async def test_uncertain_write_responses_are_never_retried(self) -> None:
        cases = [("oversized success", 200, b"oversized", 4)]
        for status in (500, 502, 504):
            cases.extend(
                (
                    (f"HTTP {status}", status, b'{"message":"Synthetic server error"}', 1024),
                    (f"oversized HTTP {status}", status, b"oversized", 4),
                )
            )

        for name, status, body, response_limit in cases:
            with self.subTest(case=name):
                attempts = 0

                def handler(_: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    return httpx.Response(status, content=body)

                _, transport = self.make_client(handler)
                with patch("confluence_connector.remote.transport.MAX_JSON_BYTES", response_limit):
                    with self.assertRaisesRegex(UncertainWrite, "inspect before retrying"):
                        await transport.send_json(
                            "POST",
                            "rest/api/content",
                            body={"title": "Synthetic"},
                            expected_username="sample-user",
                        )
                self.assertEqual(attempts, 1)

    async def test_write_retries_only_retry_after_429_or_503(self) -> None:
        for status in (429, 503):
            with self.subTest(status=status):
                attempts = 0
                sleep = AsyncMock()

                def handler(request: httpx.Request) -> httpx.Response:
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        return httpx.Response(status, headers={"Retry-After": "0"}, json={})
                    return httpx.Response(200, json={"created": True})

                _, transport = self.make_client(handler, sleep=sleep)
                payload = await transport.send_json(
                    "POST", "rest/api/content", body={"title": "Synthetic"}, expected_username="sample-user"
                )
                self.assertTrue(payload["created"])
                self.assertEqual(attempts, 2)
                sleep.assert_awaited_once_with(0.0)

    async def test_persistent_read_500_is_reported_after_three_attempts(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(500, json={"message": "Synthetic log reference"})

        _, transport = self.make_client(handler)
        with self.assertRaisesRegex(Upstream, "Synthetic log reference"):
            await transport.get_json("rest/api/user/current")
        self.assertEqual(attempts, 3)

    async def test_read_429_honors_retry_after(self) -> None:
        attempts = 0
        sleep = AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "2.5"}, json={})
            return httpx.Response(200, json={"ok": True})

        _, transport = self.make_client(handler, sleep=sleep)
        await transport.get_json("rest/api/user/current")
        sleep.assert_awaited_once_with(2.5)

    async def test_long_retry_after_is_reported_instead_of_waited_for(self) -> None:
        attempts = 0
        sleep = AsyncMock()

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(429, headers={"Retry-After": "3600"}, json={})

        _, transport = self.make_client(handler, sleep=sleep)
        with self.assertRaises(Throttled):
            await transport.get_json("rest/api/user/current")
        self.assertEqual(attempts, 1)
        sleep.assert_not_awaited()

    async def test_write_connect_failure_is_safe_to_retry(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("synthetic connection failure", request=request)
            return httpx.Response(200, json={"updated": True})

        _, transport = self.make_client(handler)
        payload = await transport.send_json(
            "PUT", "rest/api/content/123", body={"title": "Synthetic"}, expected_username="sample-user"
        )
        self.assertTrue(payload["updated"])
        self.assertEqual(attempts, 2)

    async def test_account_is_read_from_store_for_each_request(self) -> None:
        credentials = FakeCredentialSource()
        observed_tokens: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed_tokens.append(request.headers["Authorization"])
            return httpx.Response(200, json={"ok": True})

        _, transport = self.make_client(handler, credentials=credentials)
        await transport.get_json("rest/api/user/current", expected_username="sample-user")
        self.assertEqual(observed_tokens, ["Bearer synthetic-pat"])
        self.assertGreaterEqual(credentials.loads, 2)

    async def test_account_switch_is_rejected_before_a_request(self) -> None:
        credentials = FakeCredentialSource(username="sample-user-b")
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, json={})

        _, transport = self.make_client(handler, credentials=credentials)
        with self.assertRaises(AccountMismatch):
            await transport.get_json("rest/api/user/current", expected_username="sample-user-a")
        self.assertEqual(attempts, 0)

    async def test_sign_out_during_request_prevents_returning_the_result(self) -> None:
        credentials = FakeCredentialSource()

        def handler(request: httpx.Request) -> httpx.Response:
            credentials.credential = None
            return httpx.Response(200, json={"private": "synthetic"})

        _, transport = self.make_client(handler, credentials=credentials)
        with self.assertRaises(AuthenticationRequired):
            await transport.get_json("rest/api/user/current", expected_username="sample-user")

    async def test_account_change_after_write_response_reports_uncertain_outcome(self) -> None:
        cases = [
            ("sign out", None),
            ("switch account", StoredCredential(username="sample-user-b", token="synthetic-pat-b")),
        ]
        for name, new_credential in cases:
            with self.subTest(change=name):
                credentials = FakeCredentialSource()

                def handler(_: httpx.Request) -> httpx.Response:
                    credentials.credential = new_credential
                    return httpx.Response(200, json={"id": "700"})

                _, transport = self.make_client(handler, credentials=credentials)
                with self.assertRaisesRegex(UncertainWrite, "inspect the target before retrying"):
                    await transport.send_json(
                        "POST",
                        "rest/api/content",
                        body={"title": "Synthetic"},
                        expected_username="sample-user",
                    )

    async def test_status_mapping_and_error_detail(self) -> None:
        _, transport = self.make_client(lambda _: httpx.Response(409, json={"message": "Version conflict"}))
        with self.assertRaisesRegex(Conflict, "version or resource conflict"):
            await transport.get_json("rest/api/content/123")

    async def test_current_user_uses_candidate_token_before_persistence(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request.headers["Authorization"])
            return httpx.Response(200, json={"username": "sample-user"})

        credentials = FakeCredentialSource()
        credentials.credential = None
        _, transport = self.make_client(handler, credentials=credentials)
        api = ConfluenceApi(settings(), transport)
        user = await api.current_user(token_override="candidate-pat")
        self.assertEqual(user.username, "sample-user")
        self.assertEqual(observed, ["Bearer candidate-pat"])
        self.assertEqual(credentials.loads, 0)

    async def test_spaces_use_actual_size_not_echoed_limit_or_next_link(self) -> None:
        starts: list[tuple[str, int]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            start = int(request.url.params["start"])
            status = request.url.params["status"]
            starts.append((status, start))
            if status == "archived":
                entries = [{
                    "id": 501,
                    "key": "ARCHIVED",
                    "name": "Archived Synthetic Space",
                    "type": "global",
                    "status": "archived",
                }]
                total_size = 1
            elif start == 0:
                entries = [
                    {
                        "id": index,
                        "key": f"SPACE-{index}",
                        "name": f"Synthetic Space {index}",
                        "type": "global",
                        "status": "current",
                        "_links": {"webui": f"/wiki/display/SPACE-{index}/home"},
                    }
                    for index in range(500)
                ]
                total_size = 501
            else:
                entries = [{"id": 500, "key": "SPACE-500", "name": "Last Synthetic Space"}]
                total_size = 501
            return httpx.Response(
                200,
                json={
                    "results": entries,
                    "start": start,
                    "size": len(entries),
                    "limit": 500,
                    "totalSize": total_size,
                    "_links": {"next": "https://elsewhere.example.test/skip-this"},
                },
            )

        _, transport = self.make_client(handler)
        api = ConfluenceApi(settings(), transport)
        result = await api.list_spaces(expected_username="sample-user")
        self.assertEqual(starts, [("current", 0), ("current", 500), ("archived", 0)])
        self.assertEqual(len(result.spaces), 502)
        self.assertEqual(result.total_count, 502)
        self.assertFalse(result.truncated)
        self.assertEqual(result.spaces[0].url, "https://confluence.example.test/wiki/display/SPACE-0/home")
        self.assertEqual(result.spaces[-1].status, "archived")

    async def test_spaces_report_malformed_actual_size(self) -> None:
        _, transport = self.make_client(
            lambda _: httpx.Response(200, json={"results": [{"key": "SPACE", "name": "Synthetic"}], "size": 2})
        )
        with self.assertRaisesRegex(Upstream, "inconsistent collection page offset or size"):
            await ConfluenceApi(settings(), transport).list_spaces(expected_username="sample-user")

    async def test_display_url_resolves_page_id_by_space_and_title(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"results": [{"id": "789"}]})

        _, transport = self.make_client(handler)
        api = ConfluenceApi(settings(), transport)
        resolver = PageLinkResolver(settings(), transport, api)
        result = await resolver.resolve(
            "https://confluence.example.test/wiki/display/ENG/Synthetic+Page?pageVersion=4#comments",
            expected_username="sample-user",
        )
        self.assertEqual((result.page_id, result.version), ("789", 4))
        self.assertEqual(calls[0].url.params["spaceKey"], "ENG")
        self.assertEqual(calls[0].url.params["title"], "Synthetic Page")

    async def test_display_url_uses_exact_escaped_title(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request.url.params["title"])
            return httpx.Response(200, json={"results": [{"id": "789"}]})

        _, transport = self.make_client(handler)
        api = ConfluenceApi(settings(), transport)
        resolver = PageLinkResolver(settings(), transport, api)
        await resolver.resolve(
            "https://confluence.example.test/wiki/display/ENG/Guide%20O%27Brien",
            expected_username="sample-user",
        )
        self.assertEqual(observed[0], "Guide O'Brien")

    async def test_display_url_after_rename_fails_clearly(self) -> None:
        _, transport = self.make_client(lambda _: httpx.Response(200, json={"results": []}))
        resolver = PageLinkResolver(settings(), transport, ConfluenceApi(settings(), transport))
        with self.assertRaisesRegex(NotFound, "may have been renamed"):
            await resolver.resolve(
                "https://confluence.example.test/wiki/display/ENG/Old+Title",
                expected_username="sample-user",
            )

    async def test_short_link_follows_only_a_same_instance_redirect(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(str(request.url))
            return httpx.Response(
                302,
                headers={
                    "Location": "https://confluence.example.test/wiki/spaces/ENG/pages/246/Synthetic?pageVersion=2"
                },
            )

        _, transport = self.make_client(handler)
        resolver = PageLinkResolver(settings(), transport, ConfluenceApi(settings(), transport))
        result = await resolver.resolve(
            "https://confluence.example.test/wiki/x/short?pageVersion=5",
            expected_username="sample-user",
        )
        self.assertEqual((result.page_id, result.version), ("246", 5))
        self.assertEqual(observed, ["https://confluence.example.test/wiki/x/short?pageVersion=5"])

    async def test_page_id_url_forms_ignore_title_anchor_and_comment_focus(self) -> None:
        _, transport = self.make_client(lambda _: httpx.Response(200, json={}))
        resolver = PageLinkResolver(settings(), transport, ConfluenceApi(settings(), transport))
        values = (
            ("123", "123", None),
            (
                "https://confluence.example.test/wiki/spaces/ENG/pages/234/Any+Title?pageVersion=7#section",
                "234",
                7,
            ),
            (
                "https://confluence.example.test/wiki/pages/viewpage.action?pageId=345&pageVersion=3&focusedCommentId=4",
                "345",
                3,
            ),
        )
        for reference, expected_id, expected_version in values:
            with self.subTest(reference=reference):
                result = await resolver.resolve(reference, expected_username="sample-user")
                self.assertEqual((result.page_id, result.version), (expected_id, expected_version))

    async def test_external_host_and_out_of_context_urls_are_refused_before_network(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, json={})

        _, transport = self.make_client(handler)
        resolver = PageLinkResolver(settings(), transport, ConfluenceApi(settings(), transport))
        for value in (
            "https://other.example.test/wiki/spaces/ENG/pages/123/title",
            "https://confluence.example.test/elsewhere/spaces/ENG/pages/123/title",
        ):
            with self.subTest(value=value), self.assertRaises(InvalidRequest):
                await resolver.resolve(value, expected_username="sample-user")
        self.assertEqual(attempts, 0)

    async def test_malformed_page_version_is_refused(self) -> None:
        _, transport = self.make_client(lambda _: httpx.Response(200, json={}))
        resolver = PageLinkResolver(settings(), transport, ConfluenceApi(settings(), transport))
        with self.assertRaisesRegex(InvalidRequest, "positive integer"):
            await resolver.resolve(
                "https://confluence.example.test/wiki/spaces/ENG/pages/123/title?pageVersion=0",
                expected_username="sample-user",
            )


if __name__ == "__main__":
    unittest.main()
