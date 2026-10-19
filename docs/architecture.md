# Confluence connector architecture

[Architecture overview](diagrams/architecture.svg) · [Code map](diagrams/code-map.svg)

Current implementation. Intended capabilities are in [requirements](requirements.md); open work is in
the [roadmap](roadmap.md).

## 1. Entry points and lifetime

`confluence-connector` runs `confluence_connector.__main__:main`. Its commands are `auth`,
`auth --sign-out`, `status [--check]`, and `mcp`. Exit codes are 0 for success, 1 for other errors,
2 when authentication is required, and 130 for interruption.

Each MCP process has one lazy `bootstrap.AppContext`, one async HTTP/1.1 client, and one encrypted
credential store. Adapters are rebuilt when the stored username changes. The token is read from
encrypted storage for every request, and the active account is checked again after each response.
There is no daemon, scheduler, web surface, or persistent content store.

The MCP surface currently exposes six read-only tools:

| Tool | Implemented behavior |
|---|---|
| `auth_status` | Offline configured-instance, stored-token, and account-name status |
| `list_spaces` | Current and archived visible spaces with IDs, keys, names, types, statuses and same-instance links |
| `search_content` | Literal CQL search with structured scope, approximate totals, stateless cursors, and labelled quick-navigation title matches |
| `find_users` | Current users by display-name query; former users by exact username |
| `get_page_tree` | Ordered page children and ancestors from a page or space root, with bounded depth and result size |
| `get_page_metadata` | One or more page histories, retained versions, labels, contributors and ancestors |

Page text reads, comments, attachment downloads, exports and write tools are not implemented yet.

## 2. Modules and boundaries

- `src/confluence_connector/__main__.py`: CLI parsing, configuration selection, authentication and
  process exit codes.
- `src/confluence_connector/bootstrap.py`: lazy process composition and account-bound services.
- `src/confluence_connector/config.py`: required private settings, loader precedence, product constants
  and the per-user data directory.
- `src/confluence_connector/logs.py`: process-scoped rotating logs with credential redaction.
- `src/confluence_connector/auth/store.py`: encrypted PAT and username persistence under
  `CrossPlatLock`.
- `src/confluence_connector/domain/`: connector errors and typed status, space, search, user, tree and metadata results.
- `src/confluence_connector/remote/transport.py`: async HTTP, retries, response limits and account
  checks; it never logs request URLs or payloads.
- `src/confluence_connector/remote/confluence.py`: configured current-user, space and search routes;
  quick navigation, user lookup, ordered children and page-history mapping.
- `src/confluence_connector/remote/links.py`: page ID and URL resolution for tree, metadata and later page tools; it
  refuses URLs outside the configured host and context path.
- `src/confluence_connector/service/spaces.py`: offline auth status, verified status and space listing.
- `src/confluence_connector/service/search.py`: query and scope validation, literal CQL, title-source
  merging and self-contained search cursors.
- `src/confluence_connector/service/users.py`: display-name paging and exact username lookups.
- `src/confluence_connector/service/tree.py`: page/space roots, ordered child paging and depth/page caps.
- `src/confluence_connector/service/metadata.py`: bounded batch resolution and per-page metadata failures.
- `src/confluence_connector/service/cursors.py`: strict, process-independent cursor encoding and decoding.
- `src/confluence_connector/surfaces/mcp_main.py`: annotated MCP tools and tool-call timing logs.
- `research/probe_auth.py`: separate manual probe that reuses configuration but never persists secrets.

The HTTP adapter owns REST route spelling and wire formats. The service owns account-bound capability
calls. MCP declarations stay thin and do not interpret page content.

## 3. Configuration and authentication

The required private JSON contains `base_url`, `query`, `form_auth_path`, and the configured endpoint
paths. The probe-only `query` and `form_auth_path` remain in the shared loader contract. `--config PATH`
takes precedence over `CONFLUENCE_CONFIG`, followed by repository-root `confluence_config.json`,
independent of the working directory. Missing or invalid settings fail before a prompt or network call.
Settings are cached in each process; restart after edits.

`APP_NAME`, `CLI_NAME`, `MCP_SERVER_NAME`, and the log-level environment variable are named in
`config.py`. Local state lives at `%USERPROFILE%\.lrh-confluence-connector` on Windows, under the XDG
data directory elsewhere, or at `CONFLUENCE_CONNECTOR_HOME` when explicitly overridden.

The CLI prompts for a user-generated PAT without echoing it, checks it with the configured
`/rest/api/user/current` endpoint, and only then stores the token and returned username together in an
encrypted `msal-extensions` persistence record. `CrossPlatLock` coordinates reads, sign-in completion
and sign-out. There is no plaintext fallback. A different username is refused until the user runs
`auth --sign-out`, making account switching explicit. The Confluence credential is unrelated to
Microsoft OAuth.

`status` reads local state only. `status --check` verifies the token against the current-user endpoint.
The MCP `auth_status` tool is offline and reports the stored account name; it does not claim that the
server still accepts the token. Sign-out clears the local credential and does not revoke it remotely.

## 4. HTTP, paging and links

The process-scoped `httpx.AsyncClient` disables redirects and HTTP/2, permits up to six connections,
and uses 10-second connect/pool and 30-second read/write timeouts. The transport limits in-flight
requests to six. Reads retry up to three attempts for connection failures, timeouts, 429, 500, 502,
503 and 504 responses, honoring `Retry-After` or using short jittered backoff. Writes retry only after
a connection failure before sending or an explicit 429/503 response. A write timeout, incomplete
response or 504 is reported as uncertain and is not resent.

Space results request up to 500 entries for each status (`current` and `archived`). Paging advances
using the returned `start + size`; the connector does not use the echoed `limit` or a server-provided
`next` URL. Invalid offsets, sizes, totals and duplicate spaces fail with an upstream error instead
of silently dropping entries.

Search sends only CQL constructed from literal terms and validated scope values. `match=all` creates
one `siteSearch` or `title` clause per term joined by AND; `any` sends the terms together; `phrase`
preserves their order. Query punctuation is retained inside terms and stripped at their edges. Title
search replaces punctuation with spaces and adds the observed quick-navigation source on the first
page; each hit reports its source. Quick-navigation failures leave CQL results usable. Cursors carry
the normalized request and actual next offset, are revalidated on use, and do not depend on process
memory. Full pages can have a continuation even when Confluence's approximate total suggests otherwise.

Search scope uses `space_keys`, `space_type`, `content_types`, `labels`, `creator_usernames`,
`contributor_usernames`, `created_after/before`, `modified_after/before`, `parent`, `ancestor` and
`include_archived` (true by default). Dates with offsets are normalized to UTC. Page/space trees keep
the server's child order, advance by actual `start + size`, and stop at depth 5 or 1,000 returned pages.
Metadata accepts at most 20 references; experimental page versions are read in 200-item pages and
capped at 5,000 per page record. It describes the current page and refuses a versioned reference;
historical metadata reads are not implemented. Results identify truncation or per-reference failures.

`PageLinkResolver` accepts numeric IDs and the documented `/spaces/KEY/pages/ID/…`,
`viewpage.action?pageId=`, `/x/…`, and `/display/KEY/Title` forms. Display URLs resolve by exact title
inside their space, and a renamed title fails clearly. Short-link redirects are followed manually and
must stay on the configured HTTPS origin and context path. `pageVersion` is preserved; anchors and
comment focus are ignored. Search scope, page trees and metadata use this resolver for page references;
page text reading is still open work.

Operational logs include tool names, outcomes, timings and counts only. HTTP library URL logging is
quieted because URLs and response content can carry private data.

## 5. Research and checks

The probe supports anonymous, PAT, Basic, form-session and supplied OAuth-bearer modes. Form mode is a
single interactive research request; OAuth transports an already obtained token and does not implement
authorization. No probe runs on application startup.

Run offline checks from the repository root:

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research tests
uv run python research/probe_auth.py --help
```

No live Confluence request is a routine check. See [API research](api-research.md) for dated evidence
and [research/README.md](../research/README.md) for explicitly started probes.
