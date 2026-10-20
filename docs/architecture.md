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

The MCP surface currently exposes seven read-only tools:

| Tool | Implemented behavior |
|---|---|
| `auth_status` | Offline configured-instance, stored-token, and account-name status |
| `list_spaces` | Current and archived visible spaces with IDs, keys, names, types, statuses and same-instance links |
| `search_content` | Literal CQL search with structured scope, approximate totals, stateless cursors, and labelled quick-navigation title matches |
| `find_users` | Current users by display-name query; former users by exact username |
| `get_page_tree` | Ordered page children and ancestors from a page or space root, with bounded depth and result size |
| `get_page_metadata` | One or more page histories, retained versions, labels, contributors and ancestors |
| `get_pages` | Budgeted text or unchanged storage reads, typed references, optional attachment/comment sections, and version-pinned continuation |

Attachment downloads, exports and write tools are not implemented yet.

## 2. Modules and boundaries

- `src/confluence_connector/__main__.py`: CLI parsing, configuration selection, authentication and
  process exit codes.
- `src/confluence_connector/bootstrap.py`: lazy process composition and account-bound services.
- `src/confluence_connector/config.py`: required private settings, optional CQL timezone, loader
  precedence, atomic calibration updates, product constants and the per-user data directory.
- `src/confluence_connector/timezones.py`: IANA/fixed-offset validation and measured-offset formatting.
- `src/confluence_connector/logs.py`: process-scoped rotating logs with credential redaction.
- `src/confluence_connector/auth/store.py`: encrypted PAT and username persistence under
  `CrossPlatLock`.
- `src/confluence_connector/domain/`: connector errors and typed status, search, page, attachment, comment and reference results.
- `src/confluence_connector/remote/transport.py`: async HTTP, retries, response limits and account
  checks; it never logs request URLs or payloads.
- `src/confluence_connector/remote/confluence.py`: configured current-user, space and search routes;
  quick navigation, user lookup, ordered children, page bodies, attachments, comments and history mapping.
- `src/confluence_connector/remote/links.py`: page ID and URL resolution for tree, metadata and page reads; it
  refuses URLs outside the configured host and context path.
- `src/confluence_connector/service/spaces.py`: offline auth status, verified status and space listing.
- `src/confluence_connector/service/search.py`: query and scope validation, literal CQL, title-source
  merging and self-contained search cursors.
- `src/confluence_connector/service/cql_dates.py`: UTC scope normalization, configured-zone conversion,
  inclusive minute rounding and daylight-saving edge handling.
- `src/confluence_connector/service/timezone_calibration.py`: bounded read-only CQL search that
  measures the effective CQL offset against a page's UTC modification time.
- `src/confluence_connector/service/users.py`: display-name paging and exact username lookups.
- `src/confluence_connector/service/tree.py`: page/space roots, ordered child paging and depth/page caps.
- `src/confluence_connector/service/metadata.py`: bounded batch resolution and per-page metadata failures.
- `src/confluence_connector/service/pages.py`: page-reference validation, body/section reads, character budgets and version-pinned cursors.
- `src/confluence_connector/service/page_text.py`: storage-source identity extraction and block-based HTML-to-text conversion.
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
Settings are cached in each process; restart after edits or a CLI calibration writes `cql_timezone`.

`APP_NAME`, `CLI_NAME`, `MCP_SERVER_NAME`, and the log-level environment variable are named in
`config.py`. Local state lives at `%USERPROFILE%\.lrh-confluence-connector` on Windows, under the XDG
data directory elsewhere, or at `CONFLUENCE_CONNECTOR_HOME` when explicitly overridden.

The CLI prompts for a user-generated PAT without echoing it, checks it with the configured
`/rest/api/user/current` endpoint, and only then stores the token and returned username together in an
encrypted `msal-extensions` persistence record. `CrossPlatLock` coordinates reads, sign-in completion
and sign-out. There is no plaintext fallback. A different username is refused until the user runs
`auth --sign-out`, making account switching explicit. The Confluence credential is unrelated to
Microsoft OAuth.

`status` reads local state only. `auth` calibrates the CQL timezone after verifying and storing the
candidate token; `status --check` verifies the stored token and then calibrates it. Calibration uses a
newest readable page and a bounded binary search of `lastmodified` minute cutoffs. It runs only when a
user invokes these CLI commands, never on MCP startup or a tool call. If `cql_timezone` is absent,
`config.py` writes the measured fixed offset atomically under a cross-process lock, preserving other
settings. If set, only `status --check` compares the configured zone's offset at the calibration
instant and warns on a mismatch without overwriting it. The automatically written fixed offset does
not follow daylight saving; users replace it with an IANA zone when needed. The MCP `auth_status` tool
is offline and reports the stored account name; it does not claim that Confluence still accepts the
token. Sign-out clears the local credential and does not revoke it remotely.

The optional private `cql_timezone` setting accepts an IANA name or a fixed `UTC±HH:MM` offset. The
example config leaves it `null` so the next user-run `auth` or `status --check` can measure it.

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
page only when no scope filters are set; CQL hits keep their order and the merged page is capped at the
requested limit. Each hit reports its source, and cursors remember only quick-navigation IDs included
in the returned page. Quick-navigation failures leave CQL results usable, while account changes still
fail the call. Cursors carry the normalized request and actual next offset, are revalidated on use, and
do not depend on process memory. Full pages can have a continuation even when Confluence's approximate
total suggests otherwise.

Search scope uses `space_keys`, `space_type`, `content_types`, `labels`, `creator_usernames`,
`contributor_usernames`, `created_after/before`, `modified_after/before`, `parent`, `ancestor` and
`include_archived` (true by default). Date-only values pass through as `yyyy/MM/dd` and mean that
calendar day in Confluence's timezone; mixed date and timestamp bounds are ordered against that whole
local day. Timestamps with offsets are normalized to UTC; timestamps
without offsets are treated as UTC. Timestamp bounds require `cql_timezone`, convert to timezone-less
`yyyy/MM/dd HH:mm`, and round lower bounds down and upper bounds up to preserve inclusive minutes.
Repeated daylight-saving hours are conservatively widened because CQL literals cannot identify a
fold; a rounded minute in a spring-forward gap moves to the next valid minute. Cursors store the
original UTC instants and reconvert them on every page. If the setting is missing, date-only search
continues to work and timestamp filters return an actionable `InvalidRequest`. Space-root tree reads
request `depth=root`, then child reads follow each page's direct-child route. Trees keep the server's
child order, advance by actual `start + size`, and stop at depth 5 or 1,000 returned pages.
Metadata accepts at most 20 references; experimental page versions are read in 200-item pages and
capped at 5,000 per page record. It describes the current page and refuses a versioned reference;
historical metadata reads are not implemented. Results identify truncation or per-reference failures.

`get_pages` reads each page body, attachment list and comment thread as up to three parallel requests;
the shared transport caps all requests at six in flight. `body.view` supplies resolved text, while
`body.storage` supplies Jira, PlantUML, HTML-macro and ordered reference metadata. The storage parser
preserves CDATA in plain-text macro bodies, including PlantUML source and code fallback content. Text
output keeps headings, ordered-list numbering from `start` and `value`, unordered lists, code, panels,
status, expand blocks, links and pipe tables; panel children are rendered as blocks before quoting so
paragraphs and list markers remain distinct. Multi-paragraph list items keep block boundaries and
indent continuation lines. Complex table fallbacks emit one minimal-HTML block per outer row, preserving
image, macro and link markers plus their typed references. Attachment and image identity is mapped from
source fields before markers are rendered.
Recognized HTML-macro wrappers are marked inline; storage HTML macros without a matching wrapper are
marked after the rendered body because visible text and scripts are not reliable identity sources.
Macro wrappers inside resolved includes do not consume the requested page's storage metadata. Explicit
HTML line breaks remain line breaks. Comment bodies share the page budget in both formats, while rich
page text stays unchanged and structured comments accompany only selected comment blocks.
Comments are converted from their own `view` bodies using the configured base URL for context-relative
links, then threaded by source ancestry. Typed references remain alongside text so agents do not have
to parse markers. A `rich` read returns the storage body verbatim. Both formats use block-boundary
budgets. Cursors carry page ID, version and block offset and
re-read the historical version when the current page has changed. Local root-relative links are
normalized to the configured context path before they are emitted. Attachments and comments are read
again on continuation, so those live sections can change while the page body remains pinned.

`PageLinkResolver` accepts numeric IDs and the documented `/spaces/KEY/pages/ID/…`,
`viewpage.action?pageId=`, `/x/…`, and `/display/KEY/Title` forms. Display URLs resolve by exact title
inside their space, and a renamed title fails clearly. Short-link redirects are followed manually and
must stay on the configured HTTPS origin and context path. `pageVersion` is preserved; anchors and
comment focus are ignored. Search scope, page trees, metadata and page text reads use this resolver for
page references.

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
