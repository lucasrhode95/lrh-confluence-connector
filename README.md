# Confluence connector PoC

Read-only MCP server for the Confluence Data Center wiki. It uses the documented REST API for page search, space listing, and page retrieval. It does not use the browser login endpoint observed in the HAR.

## Authentication choices

- **PAT:** documented bearer-token authentication. A PAT is revocable independently of your password; create it through the Wiki UI if your account and site policy allow it.
- **HTTP Basic:** username and password sent to documented REST endpoints. The instance may disable this.
- **Web-form session:** the captures show the Wiki's `/rest/tsv/1.0/authenticate` endpoint accepting username/password and the resulting browser session accessing search. This endpoint is not documented as a supported integration API, so it is only included in the probe.
- **OAuth with SSO:** Confluence Data Center documents authorization code with PKCE, but native OAuth API compatibility is unresolved: earlier versioned REST-reference notices differ from the provider guide's bearer-content example. Administrator registration and a live test are needed; PAT success does not prove OAuth compatibility.

A user-run PAT probe returned HTTP 200 for current user, both search routes, spaces and content-by-ID. Full dated findings and the endpoint catalog are in [research/README.md](research/README.md).

The connector itself currently supports PAT or HTTP Basic. It does not store credentials. The probe below prompts securely and does not persist credentials or cookies.

## Run the authentication probe

Requires the project's Python environment (`uv sync` once if needed). Run one mode per invocation:

    uv run python src/probe_auth.py --mode anonymous
    uv run python src/probe_auth.py --mode basic
    uv run python src/probe_auth.py --mode form
    uv run python src/probe_auth.py --mode pat
    uv run python src/probe_auth.py --mode oauth

`basic` and `form` prompt for a username and hidden password. `pat` and `oauth` prompt for a hidden bearer token; for OAuth, first obtain a token through an administrator-registered Confluence app. The script checks current-user identity, both the documented CQL endpoint and the browser UI search route, space listing, and one page retrieval when search returns a result. It prints statuses and result counts, never page text, names, identifiers, credentials, or cookie values.

The `form` mode makes one login attempt with `rememberMe` disabled by default, then tests whether the returned session works for read-only API calls. Add `--remember-me` to inspect the Wiki remember-me cookie behavior; the probe still keeps the cookie only in memory. It also copies the cookie jar into a fresh in-memory HTTP client to test immediate session replay. Nothing is written to disk. A failed form login is not retried.

## Run the MCP server

Preferred: create a Confluence PAT and set `CONFLUENCE_PAT`.

Alternative: set `CONFLUENCE_USERNAME` and `CONFLUENCE_PASSWORD` if HTTP Basic is enabled for the REST API.

    $env:CONFLUENCE_URL = "https://wiki.example.com"
    uv run confluence-connector

The server speaks MCP over stdio. Add it to your local MCP client using command `uv` and arguments `run`, `--directory`, this project directory, and `confluence-connector`. Pass `CONFLUENCE_URL` and the selected auth variables through that client's local environment configuration. Do not put real secrets in this repository, source control, or shared MCP configuration.

## Tools

- `confluence_search_pages(query, limit)`: CQL text search for pages the authenticated user can access.
- `confluence_list_spaces(limit, start)`: one page of spaces visible to the authenticated user.
- `confluence_get_page(page_id)`: page title, space, version, URL, and storage-format body.

Search and spaces calls return compact result objects. Page bodies are returned in Confluence storage format, so macros may appear as markup. All connector tools are read-only.

## Repository publication

Publish source code and sanitized research only. The Git exclusions cover raw network captures/archives, environment secrets, local MCP/editor configuration, encrypted credential files, token/cookie state, databases, downloaded content and probe logs. Real Wiki captures must stay outside Git, including as test fixtures. Keep `uv.lock` and the sanitized research catalogs versioned; review any explicit `.env.example` or `.env.template` before adding it.
