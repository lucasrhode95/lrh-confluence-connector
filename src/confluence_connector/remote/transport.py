"""Bounded asynchronous HTTP/1.1 transport for Confluence REST requests."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Mapping
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any, BinaryIO
from urllib.parse import urljoin

import httpx

from confluence_connector import config
from confluence_connector.domain.errors import (
    AccountMismatch,
    AuthenticationRequired,
    Conflict,
    InvalidRequest,
    NotFound,
    PermissionDenied,
    Throttled,
    UncertainWrite,
    Upstream,
)
from confluence_connector.remote.ports import CredentialSource

MAX_ATTEMPTS = 3
MAX_JSON_BYTES = 16 * 1024 * 1024
READ_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
WRITE_RETRY_STATUSES = frozenset({429, 503})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_log = logging.getLogger(__name__)


class ConfluenceTransport:
    """Send requests to validated configured routes without retaining credentials in memory.

    The application config is validated by ``load_config`` at CLI startup. Internal adapters pass
    relative routes and already-validated account names; this boundary rejects absolute paths.
    """

    def __init__(
        self,
        settings: Mapping[str, Any],
        credentials: CredentialSource,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._base_url = str(settings["base_url"]).rstrip("/") + "/"
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=6),
            http1=True,
            http2=False,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._limit = asyncio.Semaphore(6)
        self._sleep = sleep

    async def aclose(self) -> None:
        """Close the process-scoped HTTP client when the composition root owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        token_override: str | None = None,
        expected_username: str | None = None,
    ) -> dict[str, Any]:
        """GET one JSON route; the read may be retried according to the shared transport policy."""
        response = await self._exchange(
            "GET", path, params=params, token_override=token_override, expected_username=expected_username
        )
        return _json_payload(response)

    async def download_to(
        self,
        path: str,
        destination: BinaryIO,
        *,
        params: Mapping[str, str] | None = None,
        expected_username: str,
    ) -> tuple[str | None, int]:
        """Stream one selected attachment to a caller-owned file with the read retry policy.

        The caller supplies a seekable temporary or exclusively claimed file and removes it if this
        operation fails. A partial response is truncated before a safe GET retry.
        """
        _validate_route(path)
        for attempt in range(MAX_ATTEMPTS):
            token = self._credential(None, expected_username)
            request = self._client.build_request(
                "GET",
                path,
                params=params,
                headers={"Accept": "*/*", "Authorization": f"Bearer {token}"},
            )
            response: httpx.Response | None = None
            retry_delay: float | None = None
            try:
                async with self._limit:
                    response = await self._client.send(request, stream=True)
                    if response.status_code in READ_RETRY_STATUSES and attempt + 1 < MAX_ATTEMPTS:
                        retry_delay = _retry_delay(response.headers, attempt)
                    elif response.status_code in REDIRECT_STATUSES:
                        await response.aclose()
                        response = None
                        raise AuthenticationRequired(
                            "Confluence redirected the attachment request; the stored token was not accepted."
                        )
                    elif not 200 <= response.status_code < 300:
                        response = await _read_response(response)
                    else:
                        content_type = response.headers.get("content-type")
                        size = 0
                        async for chunk in response.aiter_bytes():
                            destination.write(chunk)
                            size += len(chunk)
                        await response.aclose()
                        response = None
                        self._assert_account(expected_username)
                        return content_type, size
            except httpx.RequestError as exc:
                _clear_destination(destination)
                if attempt + 1 < MAX_ATTEMPTS:
                    _log.warning(
                        "Confluence attachment download failed; attempt=%d error=%s",
                        attempt + 1,
                        type(exc).__name__,
                    )
                    await self._sleep(_backoff(attempt))
                    continue
                raise Upstream(f"Could not complete the Confluence attachment download ({type(exc).__name__}).") from None
            finally:
                if response is not None and not response.is_closed:
                    await response.aclose()
            if retry_delay is not None:
                _clear_destination(destination)
                await self._sleep(retry_delay)
                continue
            if response is None:
                raise Upstream("The Confluence attachment request did not return a response.")
            _raise_status(response.status_code, response.content, method="GET")
        raise Upstream("The Confluence attachment download failed after the allowed attempts.")

    async def send_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
        skip_atlassian_token_check: bool = False,
        expected_username: str,
    ) -> Any:
        """Send one write request, retrying only when the response proves it was not applied."""
        response = await self._exchange(
            method,
            path,
            params=params,
            body=body,
            expected_username=expected_username,
            skip_atlassian_token_check=skip_atlassian_token_check,
        )
        try:
            return _write_payload(response)
        except Upstream:
            raise UncertainWrite(
                "Confluence may have applied the write but returned an unreadable response; inspect before retrying."
            ) from None

    async def redirect_location(
        self,
        path: str,
        *,
        expected_username: str,
        params: Mapping[str, str] | None = None,
    ) -> str | None:
        """Read a same-instance short-link route and return its redirect target without following it."""
        response = await self._exchange(
            "GET", path, params=params, expected_username=expected_username, allow_redirect=True
        )
        if response.status_code in REDIRECT_STATUSES:
            location = response.headers.get("location")
            return urljoin(str(response.url), location) if location else None
        if not 200 <= response.status_code < 300:
            _raise_status(response.status_code, response.content, method="GET")
        return None

    async def _exchange(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
        token_override: str | None = None,
        expected_username: str | None = None,
        allow_redirect: bool = False,
        skip_atlassian_token_check: bool = False,
    ) -> httpx.Response:
        _validate_route(path)
        method = method.upper()
        is_read = method in {"GET", "HEAD"}
        for attempt in range(MAX_ATTEMPTS):
            token = self._credential(token_override, expected_username)
            headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
            if skip_atlassian_token_check:
                headers["X-Atlassian-Token"] = "no-check"
            request = self._client.build_request(method, path, params=params, json=body, headers=headers)
            response: httpx.Response | None = None
            retry_delay: float | None = None
            try:
                async with self._limit:
                    response = await self._client.send(request, stream=True)
                    retryable = response.status_code in (READ_RETRY_STATUSES if is_read else WRITE_RETRY_STATUSES)
                    if retryable and attempt + 1 < MAX_ATTEMPTS:
                        retry_delay = _retry_delay(response.headers, attempt)
                    elif response.status_code in REDIRECT_STATUSES and not allow_redirect:
                        await response.aclose()
                        response = None
                        raise AuthenticationRequired(
                            "Confluence redirected the API request to a login page; the stored token was not accepted."
                        )
                    else:
                        try:
                            response = await _read_response(response)
                        except Upstream:
                            uncertain_status = (
                                200 <= response.status_code < 300
                                or response.status_code >= 500 and response.status_code != 503
                            )
                            if not is_read and uncertain_status:
                                raise UncertainWrite(
                                    "Confluence may have applied the write but returned an unreadable response "
                                    f"(HTTP {response.status_code}); inspect before retrying."
                                ) from None
                            raise
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                if attempt + 1 < MAX_ATTEMPTS:
                    _log.warning("Confluence request could not connect; attempt=%d", attempt + 1)
                    await self._sleep(_backoff(attempt))
                    continue
                raise Upstream(f"Could not reach Confluence ({type(exc).__name__}).") from None
            except httpx.RequestError as exc:
                if is_read and attempt + 1 < MAX_ATTEMPTS:
                    _log.warning("Confluence read failed; attempt=%d error=%s", attempt + 1, type(exc).__name__)
                    await self._sleep(_backoff(attempt))
                    continue
                if not is_read:
                    raise UncertainWrite(
                        f"Confluence may have applied the write ({type(exc).__name__}); inspect the page before retrying."
                    ) from None
                raise Upstream(f"Could not complete the Confluence read ({type(exc).__name__}).") from None
            finally:
                if response is not None and not response.is_closed:
                    await response.aclose()
            if retry_delay is not None:
                await self._sleep(retry_delay)
                continue
            if response is None:
                raise Upstream("The Confluence request did not return a response.")
            if not 200 <= response.status_code < 300 and not (allow_redirect and response.status_code in REDIRECT_STATUSES):
                _raise_status(response.status_code, response.content, method=method)
            if token_override is None:
                try:
                    self._assert_account(expected_username)
                except (AccountMismatch, AuthenticationRequired):
                    if not is_read:
                        raise UncertainWrite(
                            "Confluence returned a response, but local authentication changed before the write "
                            "could be confirmed; inspect the target before retrying."
                        ) from None
                    raise
            return response
        raise Upstream("The Confluence request failed after the allowed attempts.")

    def _credential(self, token_override: str | None, expected_username: str | None) -> str:
        if token_override is not None:
            return token_override
        credential = self._credentials.load()
        if credential is None:
            raise AuthenticationRequired("No Confluence token is stored.")
        if expected_username and credential.username.casefold() != expected_username.casefold():
            raise AccountMismatch("The active Confluence account changed; retry the request.")
        return credential.token

    def _assert_account(self, expected_username: str | None) -> None:
        if expected_username is None:
            return
        current = self._credentials.load()
        if current is None:
            raise AuthenticationRequired("The Confluence token was cleared while the request was running.")
        if current.username.casefold() != expected_username.casefold():
            raise AccountMismatch("The active Confluence account changed while the request was running.")


async def _read_response(response: httpx.Response) -> httpx.Response:
    """Read a bounded response body before releasing the in-flight request slot."""
    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunks) + len(chunk) > MAX_JSON_BYTES:
            await response.aclose()
            raise Upstream("Confluence returned a response larger than the connector limit.")
        chunks.extend(chunk)
    status = response.status_code
    headers = response.headers
    request = response.request
    extensions = response.extensions
    await response.aclose()
    return httpx.Response(status, headers=headers, content=bytes(chunks), request=request, extensions=extensions)


def _clear_destination(destination: BinaryIO) -> None:
    """Discard an incomplete streaming attempt before retrying the safe GET."""
    destination.seek(0)
    destination.truncate()


def _json_payload(response: httpx.Response) -> dict[str, Any]:
    if response.status_code in REDIRECT_STATUSES:
        raise AuthenticationRequired("Confluence redirected the API request to a login page.")
    if not 200 <= response.status_code < 300:
        _raise_status(response.status_code, response.content, method="GET")
    if response.status_code == 204 or not response.content:
        return {}
    try:
        payload = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Upstream("Confluence returned a non-JSON API response.") from None
    if not isinstance(payload, dict):
        raise Upstream("Confluence returned an unexpected API response.")
    return payload


def _write_payload(response: httpx.Response) -> Any:
    """Parse any JSON write response; 2xx non-JSON replies have an uncertain write outcome."""
    if response.status_code == 204 or not response.content:
        return {}
    try:
        return json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Upstream("Confluence returned a non-JSON write response.") from None


def _validate_route(path: str) -> None:
    if not path or path.startswith(("/", "\\")) or "://" in path or ".." in path.split("/"):
        raise AssertionError("Confluence adapters must pass a safe relative route.")


def _retry_delay(headers: httpx.Headers, attempt: int) -> float:
    value = headers.get("retry-after", "")
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return _backoff(attempt)


def _backoff(attempt: int) -> float:
    return 0.2 * (2**attempt) + random.uniform(0.0, 0.2)


def _raise_status(status: int, content: bytes, *, method: str) -> None:
    detail = _error_detail(content)
    suffix = f" {detail}" if detail else ""
    if status >= 500 and method not in {"GET", "HEAD"} and status != 503:
        raise UncertainWrite(
            f"Confluence returned HTTP {status} after the write was sent; inspect before retrying."
        )
    if status == 401:
        raise AuthenticationRequired("Confluence rejected the current token.")
    if status == 403:
        raise PermissionDenied("The current Confluence account does not have permission for this request.")
    if status == 404:
        raise NotFound("The selected Confluence resource was not found or is not readable.")
    if status == 409:
        raise Conflict("Confluence reported a version or resource conflict.")
    if status == 429:
        raise Throttled("Confluence is throttling requests; wait and try again.")
    if status == 400:
        raise InvalidRequest(f"Confluence rejected the request.{suffix}")
    raise Upstream(f"Confluence returned HTTP {status}.{suffix}")


def _error_detail(content: bytes) -> str | None:
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("message", "errorMessage", "logRef"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:240]
    return None
