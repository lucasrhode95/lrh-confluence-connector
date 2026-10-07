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

Requires the project's Python environment (`uv sync` once if needed).
For a fresh fork, copy [confluence_config.example.json](confluence_config.example.json) to the
ignored confluence_config.json and edit its settings. App and probes share this
required file and a single stdlib loader/validator. Set the base URL, API paths, synthetic
query and browser login path. Probes test live endpoints and do not consume capture files.
There are no embedded target, endpoint or query fallback values. Run one mode per invocation:

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

    $env:CONFLUENCE_CONFIG = (Resolve-Path ./confluence_config.json).Path
    uv run confluence-connector

The server speaks MCP over stdio. Add it to your local MCP client using command `uv` and arguments `run`, `--directory`, this project directory, and `confluence-connector`. Pass `CONFLUENCE_CONFIG` (the absolute configuration file path) and the selected auth variables through that client's local environment configuration. Do not put real secrets in this repository, source control, or shared MCP configuration.

Configuration defaults to confluence_config.json in the repository root, independent
of the current working directory. Every command accepts --config PATH; CONFLUENCE_CONFIG
provides a process-wide override. Precedence is command flag, environment variable,
then repository default. Once selected, all callers share the parsed settings in memory.
Restart the process after editing the file.

## Portability and research

The example domain is a placeholder, not a usable deployment or a fallback. Both the
MCP server and main.py demo load the ignored confluence_config.json, selected by
CONFLUENCE_CONFIG when an explicit path is needed. Restart the process after editing it:
the shared loader validates CONFIG once per process. API paths are relative to base_url,
including when Confluence is installed under a context path such as /wiki.

The same dictionary configures research. Its --config option selects another file.
The runtime file contains deployment settings only; PAT/Basic secrets continue to come from
environment variables or the demo/probe's hidden prompts. The instance URL remains in private configuration. Temporary browser captures and downloaded
bundles are inspected outside the repository during exploratory work; only endpoint findings
and sanitized observations are documented. Probes need configured endpoints and authorization,
not capture files.

The configuration is JSON data. Never execute downloaded/captured code or capture contents.

The [research guide](research/README.md) is a starting point for another instance or changed
policy: configure the target, investigate permitted authentication and read-only REST calls,
and distinguish documented APIs from observed browser/plugin paths. Recorded successes,
denials and SAML/provider behavior describe the tested environment, not all Confluence sites.
No expected personal account is fixed in code. Credentials are not persisted by this PoC.

## Tools

- `confluence_search_pages(query, limit)`: CQL text search for pages the authenticated user can access.
- `confluence_list_spaces(limit, start)`: one page of spaces visible to the authenticated user.
- `confluence_get_page(page_id)`: page title, space, version, URL, and storage-format body.

Search and spaces calls return compact result objects. Page bodies are returned in Confluence storage format, so macros may appear as markup. All connector tools are read-only.

## Repository publication

Publish source code and sanitized research only. The Git exclusions cover raw network captures/archives, environment secrets, local MCP/editor configuration, encrypted credential files, token/cookie state, databases, downloaded content and probe logs. Real Wiki captures must stay outside Git, including as test fixtures. Keep `uv.lock` and the endpoint research notes versioned; review any explicit `.env.example` or `.env.template` before adding it.
