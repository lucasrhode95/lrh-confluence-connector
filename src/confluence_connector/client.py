"""Small HTTP client for documented Confluence Data Center REST endpoints."""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote, urlsplit

import httpx


class ConfluenceClient:
    """Authenticated read-only client. Credentials come from the caller, never from browser state."""

    def __init__(
        self,
        base_url: str,
        *,
        pat: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("CONFLUENCE_URL must be an HTTPS URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("CONFLUENCE_URL must not contain credentials, a query, or a fragment.")
        has_basic_user = bool(username)
        has_basic_password = bool(password)
        if bool(pat) == (has_basic_user or has_basic_password):
            raise ValueError("Set either CONFLUENCE_PAT or both CONFLUENCE_USERNAME and CONFLUENCE_PASSWORD.")
        if has_basic_user != has_basic_password:
            raise ValueError("Set both CONFLUENCE_USERNAME and CONFLUENCE_PASSWORD for Basic authentication.")

        headers = {"Accept": "application/json"}
        auth: httpx.BasicAuth | None = None
        if pat:
            headers["Authorization"] = f"Bearer {pat}"
        else:
            auth = httpx.BasicAuth(username or "", password or "")

        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            auth=auth,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
        )

    @classmethod
    def from_environment(cls) -> ConfluenceClient:
        """Create a client from CONFLUENCE_URL and PAT or Basic-auth environment variables."""
        base_url = os.environ.get("CONFLUENCE_URL", "")
        pat = os.environ.get("CONFLUENCE_PAT")
        username = os.environ.get("CONFLUENCE_USERNAME")
        password = os.environ.get("CONFLUENCE_PASSWORD")
        if not base_url:
            raise ValueError("Set CONFLUENCE_URL.")
        return cls(base_url, pat=pat, username=username, password=password)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ConfluenceClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search_pages(self, query: str, limit: int = 10) -> dict[str, object]:
        """Search accessible pages using the documented CQL content-search endpoint."""
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty.")
        if len(query) > 200:
            raise ValueError("query must be 200 characters or fewer.")
        _validate_limit(limit)
        cql = f"type = page AND siteSearch ~ {_cql_literal(query)}"
        payload = self._get(
            "rest/api/content/search",
            params={"cql": cql, "limit": str(limit), "expand": "space,version"},
        )
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("Confluence returned an unexpected search response.")
        return {
            "results": [_search_hit(item) for item in results if isinstance(item, dict)],
            "start": payload.get("start", 0),
            "limit": payload.get("limit", limit),
            "size": payload.get("size", len(results)),
        }

    def list_spaces(self, limit: int = 50, start: int = 0) -> dict[str, object]:
        """List one page of spaces visible to the authenticated user."""
        _validate_limit(limit)
        if start < 0:
            raise ValueError("start must be zero or greater.")
        payload = self._get(
            "rest/api/space",
            params={"limit": str(limit), "start": str(start)},
        )
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("Confluence returned an unexpected spaces response.")
        return {
            "results": [_space_summary(item) for item in results if isinstance(item, dict)],
            "start": payload.get("start", start),
            "limit": payload.get("limit", limit),
            "size": payload.get("size", len(results)),
        }

    def get_page(self, page_id: str) -> dict[str, object]:
        """Fetch one page, including its storage-format body."""
        page_id = page_id.strip()
        if not page_id.isdigit():
            raise ValueError("page_id must be a numeric Confluence content ID.")
        payload = self._get(
            f"rest/api/content/{quote(page_id, safe='')}",
            params={"expand": "body.storage,space,version"},
        )
        body = payload.get("body", {})
        storage = body.get("storage", {}) if isinstance(body, dict) else {}
        space = payload.get("space", {})
        version = payload.get("version", {})
        links = payload.get("links", {})
        return {
            "id": payload.get("id"),
            "title": payload.get("title"),
            "space_key": space.get("key") if isinstance(space, dict) else None,
            "version": version.get("number") if isinstance(version, dict) else None,
            "url": links.get("webui") if isinstance(links, dict) else None,
            "body_storage": storage.get("value") if isinstance(storage, dict) else None,
        }

    def _get(self, path: str, *, params: Mapping[str, str]) -> dict[str, object]:
        try:
            response = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not reach Confluence ({type(exc).__name__}).") from None
        if response.is_redirect:
            raise RuntimeError(
                "Confluence redirected the API request to a login page. "
                "Use an enabled PAT or Basic API credential; browser SSO is not reused."
            )
        if response.status_code == 401:
            raise RuntimeError("Confluence rejected authentication. Check the credential and site policy.")
        if response.status_code == 403:
            raise RuntimeError("The authenticated Confluence user does not have permission for this request.")
        if not response.is_success:
            raise RuntimeError(f"Confluence returned HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError("Confluence returned a non-JSON API response.") from None
        if not isinstance(payload, dict):
            raise RuntimeError("Confluence returned an unexpected API response.")
        return payload


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")


def _cql_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _search_hit(item: dict[str, object]) -> dict[str, object]:
    space = item.get("space", {})
    version = item.get("version", {})
    links = item.get("links", {})
    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "space_key": space.get("key") if isinstance(space, dict) else None,
        "updated": version.get("when") if isinstance(version, dict) else None,
        "url": links.get("webui") if isinstance(links, dict) else None,
    }


def _space_summary(item: dict[str, object]) -> dict[str, object]:
    return {
        "id": item.get("id"),
        "key": item.get("key"),
        "name": item.get("name"),
        "type": item.get("type"),
        "status": item.get("status"),
    }