"""Read-only authentication probe for the Confluence instance.

Run one authentication mode per invocation. Secrets are prompted with getpass,
never accepted as command-line arguments, and never printed or saved.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import http.cookiejar
import json
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

DEFAULT_URL = "https://wiki.example.com"
DEFAULT_QUERY = "search example codex claude 123"
AUTH_PATH = "rest/tsv/1.0/authenticate"


def main() -> int:
    """Entry point: validate the site and mode, prompt for secrets, then make read-only probes."""
    args = _parse_args()
    base_url = args.base_url or os.environ.get("CONFLUENCE_URL", DEFAULT_URL)
    _validate_base_url(base_url)

    if args.mode == "anonymous":
        with _new_client(base_url) as client:
            _probe_api(client, args.query)
        return 0

    if args.mode in {"basic", "form"}:
        username = args.username or input("Confluence username: ").strip()
        if not username:
            print("A username is required.")
            return 2
        password = getpass.getpass("Confluence password (hidden): ")
        if not password:
            print("A password is required.")
            return 2
        if args.mode == "basic":
            with _new_client(base_url, auth=httpx.BasicAuth(username, password)) as client:
                _probe_api(client, args.query)
            return 0
        return _probe_form_login(base_url, username, password, args.query, args.remember_me)

    bearer_token = getpass.getpass(
        "PAT or registered OAuth access token (hidden): "
    )
    if not bearer_token:
        print("A token is required.")
        return 2
    with _new_client(
        base_url, headers={"Authorization": f"Bearer {bearer_token}"}
    ) as client:
        _probe_api(client, args.query)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("anonymous", "basic", "form", "pat", "oauth"),
        help=(
            "anonymous=public access; basic=HTTP Basic; form=Confluence web login; "
            "pat=PAT; oauth=OAuth access token from an admin-registered app"
        ),
    )
    parser.add_argument("--base-url", help="Wiki base URL (defaults to CONFLUENCE_URL or the company Wiki).")
    parser.add_argument("--username", help="Username; password is always prompted securely.")
    parser.add_argument("--query", default=DEFAULT_QUERY, help="Text used for read-only search probes.")
    parser.add_argument("--remember-me", action="store_true", help="Send rememberMe=true in form mode to inspect cookie lifetime; nothing is saved locally.")
    return parser.parse_args()


def _validate_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit("The Wiki URL must be an HTTPS URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("The Wiki URL must not contain credentials, a query, or a fragment.")


def _new_client(
    base_url: str,
    *,
    auth: httpx.Auth | None = None,
    headers: Mapping[str, str] | None = None,
    cookies: http.cookiejar.CookieJar | None = None,
) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/") + "/",
        auth=auth,
        headers={"Accept": "application/json", **(headers or {})},
        cookies=cookies,
        timeout=httpx.Timeout(15.0, connect=8.0),
        follow_redirects=False,
    )


def _probe_form_login(
    base_url: str, username: str, password: str, query: str, remember_me: bool
) -> int:
    """Entry point: make one transient web-form login attempt and probe its session."""
    print("Form login uses Confluence's web login endpoint observed in the capture.")
    print("This endpoint is undocumented; this run will not save its credentials or cookies.")
    print(f"rememberMe sent: {remember_me}")
    with _new_client(base_url) as client:
        try:
            response = client.post(
                AUTH_PATH,
                json={
                    "username": username,
                    "password": password,
                    "rememberMe": remember_me,
                    "targetUrl": "/",
                    "captchaId": "",
                },
            )
        except httpx.HTTPError as exc:
            print(f"Form login request failed ({type(exc).__name__}); no retry was made.")
            return 1

        body: object = None
        try:
            body = response.json()
        except ValueError:
            pass
        has_next = isinstance(body, dict) and isinstance(body.get("next"), str)
        print(
            f"form login: HTTP {response.status_code}; "
            f"JSON next field={'yes' if has_next else 'no'}"
        )
        if not response.is_success:
            _print_redirect(response)
            print("Stopping after this single credential attempt.")
            return 1

        _print_cookie_summary(client)
        _probe_api(client, query)
        _probe_cookie_round_trip(base_url, client, query)
    return 0


def _probe_api(client: httpx.Client, query: str) -> None:
    """Assumes a validated base URL and an already selected authentication mode."""
    print("\nRead-only API checks (no response bodies, page titles, or identity values are printed):")
    identity = _get_json(client, "current user", "rest/api/user/current")
    if identity is not None:
        print(f"  identity signal: {_identity_signal(identity)}")

    cql = f"type = page AND siteSearch ~ {_cql_literal(query)}"
    search_params = {"cql": cql, "limit": "1", "expand": "space,version"}
    search = _get_json(
        client,
        "CQL content search",
        "rest/api/content/search",
        params=search_params,
    )
    if search is not None:
        _print_result_count("CQL content search", search)

    # This route and its harmless UI options appeared in the browser capture.
    ui_search = _get_json(
        client,
        "browser UI search route",
        "rest/api/search",
        params={
            "cql": cql,
            "limit": "1",
            "start": "0",
            "src": "next.ui.search",
            "expand": "space.icon",
            "includeArchivedSpaces": "false",
        },
    )
    if ui_search is not None:
        _print_result_count("browser UI search route", ui_search)

    spaces = _get_json(
        client,
        "space listing",
        "rest/api/space",
        params={"limit": "1", "start": "0"},
    )
    if spaces is not None:
        _print_result_count("space listing", spaces)

    page_id = _first_result_id(search)
    if page_id:
        page = _get_json(
            client,
            "get one search result",
            f"rest/api/content/{page_id}",
            params={"expand": "space,version"},
        )
        if page is not None:
            print("  get one search result: content JSON returned")


def _get_json(
    client: httpx.Client,
    label: str,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
) -> dict[str, object] | None:
    try:
        response = client.get(path, params=params)
    except httpx.HTTPError as exc:
        print(f"  {label}: request failed ({type(exc).__name__})")
        return None

    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    suffix = ""
    if response.is_redirect:
        location = response.headers.get("location", "")
        parsed = urlsplit(location)
        safe_location = (parsed.netloc + parsed.path) if location else "unknown"
        suffix = f"; redirect={safe_location}"
    elif "json" not in content_type:
        suffix = "; response is not JSON (possibly a login page)"
    print(f"  {label}: HTTP {response.status_code}{suffix}")

    if not response.is_success or "json" not in content_type:
        return None
    try:
        body = response.json()
    except ValueError:
        print(f"  {label}: invalid JSON")
        return None
    if not isinstance(body, dict):
        print(f"  {label}: JSON response is not an object")
        return None
    print(f"    response keys: {', '.join(sorted(body.keys())) or '(none)'}")
    return body


def _print_result_count(label: str, body: dict[str, object]) -> None:
    results = body.get("results")
    if isinstance(results, list):
        print(f"    {label} result count: {len(results)}")
    else:
        print(f"    {label} result list: not present")


def _first_result_id(body: dict[str, object] | None) -> str | None:
    if body is None:
        return None
    results = body.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    item_id = results[0].get("id")
    return str(item_id) if item_id is not None and str(item_id).isdigit() else None


def _identity_signal(body: dict[str, object]) -> str:
    identity_names = ("username", "userKey", "userId", "key", "displayName")
    values = [body.get(name) for name in identity_names]
    present = [str(value).strip().lower() for value in values if value]
    if not present:
        return "no recognizable account identifier (inconclusive)"
    if all(value in {"anonymous", "#anonymous", "unknown"} for value in present):
        return "anonymous"
    return "account identifier present (value redacted)"


def _probe_cookie_round_trip(base_url: str, authenticated: httpx.Client, query: str) -> None:
    """Assumes the just-authenticated client owns its fresh cookie jar."""
    jar = http.cookiejar.CookieJar()
    saved_cookies = [copy.copy(cookie) for cookie in authenticated.cookies.jar]
    for cookie in saved_cookies:
        jar.set_cookie(cookie)
    if not saved_cookies:
        print("\n  cookie round-trip: no cookies were issued; no replay test available")
        return

    print("\nMemory-only cookie round-trip into a fresh HTTP client (nothing written to disk):")
    _print_cookie_summary(authenticated, prefix="  captured")
    with _new_client(base_url, cookies=jar) as replay_client:
        identity = _get_json(replay_client, "replayed current user", "rest/api/user/current")
        if identity is not None:
            print(f"    replayed identity signal: {_identity_signal(identity)}")
        cql = f"type = page AND siteSearch ~ {_cql_literal(query)}"
        search = _get_json(
            replay_client,
            "replayed CQL search",
            "rest/api/content/search",
            params={"cql": cql, "limit": "1"},
        )
        if search is not None:
            _print_result_count("replayed CQL search", search)
    print("  This proves only an immediate in-memory replay, not cookie lifetime or encryption.")


def _print_cookie_summary(client: httpx.Client, *, prefix: str = "  session") -> None:
    cookies = list(client.cookies.jar)
    persistent = sum(cookie.expires is not None for cookie in cookies)
    secure = sum(bool(cookie.secure) for cookie in cookies)
    rest_flags = [getattr(cookie, "_rest", {}) for cookie in cookies]
    http_only = sum(
        any(str(key).lower() == "httponly" for key in flags)
        for flags in rest_flags
    )
    print(
        f"{prefix} cookies: count={len(cookies)}, persistent={persistent}, "
        f"secure={secure}, httpOnly={http_only} (names and values redacted)"
    )


def _print_redirect(response: httpx.Response) -> None:
    location = response.headers.get("location", "")
    if not location:
        return
    parsed = urlsplit(location)
    print(f"  redirect target: {(parsed.netloc + parsed.path) if location else 'unknown'}")


def _cql_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        raise SystemExit(130)
