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

The MCP surface exposes eight read tools, two local-file tools and nine write tools:

| Tool | Implemented behavior |
|---|---|
| `auth_status` | Offline configured-instance, stored-token, and account-name status |
| `list_spaces` | Current and archived visible spaces with IDs, keys, names, types, statuses and same-instance links |
| `search_content` | Literal CQL search with structured scope, approximate totals, stateless cursors, and labelled quick-navigation title matches |
| `find_users` | Current users by display-name query; former users by exact username |
| `get_page_tree` | Ordered page children and ancestors from a page or space root, with bounded depth and result size |
| `get_page_metadata` | One or more page histories, retained versions, labels, contributors and ancestors |
| `get_pages` | Budgeted text or unchanged storage reads, typed references, optional attachment/comment sections, and version-pinned continuation |
| `download_attachments` | Verify selected attachment IDs belong to one page, then save each file locally with per-file failures |
| `export_pages` | Write up to 500 complete pages as a single file or flat ZIP, optionally including attachment bytes and reporting incomplete items |
| `create_page` | Create a current page or new-page draft with validated placement, body conversion and a duplicate-title preflight |
| `update_page` | Replace a page body with expected-version and content-loss checks |
| `list_drafts` | List the signed-in user's new-page drafts with bounded paging and cursors |
| `publish_draft` | Publish a new-page draft under the same page ID |
| `delete_draft` | Permanently delete a new-page draft, bypassing the trash |
| `set_labels` | Add or remove global labels with reverse compensation and explicit partial-failure results |
| `move_page` | Move a page between parents or spaces, then apply sibling position; cross-space moves poll the long task |
| `delete_page` | Preflight delete operations, preserve placement, and trash a page, direct children or bounded subtree |
| `list_trash` | Page through trashed pages for one space or all visible spaces |
| `restore_page` | Restore at the space root, then best-effort apply caller or saved placement |

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
  checks, uncertain outcomes when account state changes after a write response, and bounded streaming
  downloads; it never logs request URLs or payloads.
- `src/confluence_connector/remote/confluence.py`: configured current-user, space, search, draft,
  page-write, label, trash, restore, permission and content-property routes; quick navigation, user
  lookup, ordered children, page bodies, attachments, comments and history mapping. The UI move
  action is isolated here, reports application-level errors and polls documented cross-space tasks.
- `src/confluence_connector/remote/links.py`: page ID and URL resolution for tree, metadata, page reads and writes; it
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
- `src/confluence_connector/service/attachments.py`: selected attachment validation, same-page ownership checks, safe local filenames and per-file results.
- `src/confluence_connector/service/exports.py`: up to 500 resolved pages, full text/rich files, optional attachment downloads, bounded-memory flat ZIP packaging and failure reports.
- `src/confluence_connector/service/writes.py`: validated create/update/publish/delete-draft/label operations, placement, duplicate-title checks, draft cursors, partial results and safe label compensation.
- `src/confluence_connector/service/structure.py`: validated moves, bounded trash cursors, subtree and permission preflight, placement properties, restore fallbacks and exact partial delete results.
- `src/confluence_connector/service/write_markup.py`: text escaping, safe rich-body conversions, active-content and 128-level nesting refusal, and source-reference loss detection.
- `src/confluence_connector/service/localfiles.py`: shared seven-day output-folder cleanup, owner-only POSIX output permissions and collision-safe file claims.
- `src/confluence_connector/service/page_text.py`: storage parameters and references, the Confluence pre-pass over `view`, and markdownify-based conversion into budget blocks.
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
the shared transport caps all requests at six in flight. When storage expansion is requested, the
adapter requires a string `body.storage.value`; comment reads request `body.view`. A missing or
malformed requested value is an upstream error.

`service/page_text.py` converts bodies in three steps. First, storage is parsed with BeautifulSoup for
macro parameters (Jira keys, PlantUML source, titles, include targets) and typed references
(`ri:page`, `ri:attachment`, inline-comment markers, non-text macros). Second, a BeautifulSoup pre-pass
over `body.view` replaces each `data-macro-name` wrapper with its D3 marker, pairing wrappers with
storage macros by name and document order; wrappers inside a resolved include take no local source.
The same pass turns images and attachment links into markers with typed references (identity from
`data-linked-resource-*` attributes and the attachment list), mentions into `@Name`, inline tasks into
`[ ]`/`[x]`, normalizes same-instance links to absolute URLs under the configured context path, and
drops scripts and styles. Third, markdownify converts the result, with one override for tables:
merged cells are repeated, and tables containing nested tables or code fall back to minimal HTML.
References are collected before conversion; marker text is never parsed back. Comments are converted
the same way from their own `view` bodies and threaded by source ancestry.

The converted text is split into blocks at blank lines outside fenced code, so paragraphs, whole
lists, tables, panels and code blocks are budget units; a single oversized block is returned whole
with a note. A `rich` read returns the storage body verbatim, split between top-level elements using
the parser's source positions so the slices join back to the exact source. Comment bodies share the
page budget in both formats, and structured comments accompany only selected comment blocks. Cursors
carry page ID, version and a stable next-block anchor and re-read the historical version when the
page has changed; the adapter requires the returned source version to confirm the requested one. Body
anchors use the pinned block order, while attachment and comment anchors use their source IDs, so
insertions before a continuation point do not shift it; a removed anchored item returns an
actionable restart error. Text continuation keeps the separator before its first block so fragments
join back to the original text, and an oversized continuation state is rejected instead of issuing a
cursor the decoder would refuse.

`PageLinkResolver` accepts numeric IDs and the documented `/spaces/KEY/pages/ID/…`,
`viewpage.action?pageId=`, `/x/…`, and `/display/KEY/Title` forms. Display URLs resolve by exact title
inside their space, and a renamed title fails clearly. Short-link redirects are followed manually and
must stay on the configured HTTPS origin and context path. `pageVersion` is preserved; anchors and
comment focus are ignored. Search scope, page trees, metadata and page text reads use this resolver for
page references.

`download_attachments` accepts one page reference and up to 500 attachment IDs. The service lists that
page's attachments before downloading, so an ID cannot select a file from another page. Files are
streamed through the shared six-request transport limit into a per-account subfolder under the local
`downloads` directory. Names replace Windows-invalid path characters; an existing name receives a
numeric suffix. Failed downloads remove their partial file and are returned alongside successes.
Output files older than seven days are removed when the folder is next used.
On POSIX systems, output directories are owner-only and claimed files use owner read/write permissions.

### Page, draft and structure management

`create_page` accepts exactly one parent_id page reference or space root. It checks for a current duplicate title
before creating the page, converts the body, and uses the configured REST content route. First/before/
after positioning uses the isolated Confluence UI move action after creation; if that follow-up fails,
the result says the page remains created and placement was not confirmed. A draft receives the same
best-effort position request.

`update_page` reads the current storage body and version, refuses a stale expected version before
writing, then sends the next version so Confluence's 409 protects against a concurrent edit. The
service compares inline-comment anchors, macro names and attachment references. Losing an inline
comment anchor is refused unless the caller confirms with `allow_comment_loss`; confirmed losses
and removed macros/attachment references are returned as warnings.

`list_drafts` reads the documented content collection with `status=draft`, at most 100 rows per
request, and cursors that carry the space filter and next offset. The offset cap is 1,000,000 and
the result reports truncation if drafts remain. Publishing verifies a new-page
draft and sends its complete storage body, space and parent with version 1, retaining the ID.
`delete_draft` verifies draft status before using `DELETE ?status=draft`; this is permanent and
bypasses the trash. `set_labels` reads the current labels, skips already-satisfied changes, and
sends one request per change. On a definitive failure, confirmed changes are reversed in reverse
order. Successful reversals move into `compensated`; changes still applied remain in `added` or
`removed`, and failed reversals appear in `rollback_failures`. Uncertain write outcomes and account
changes stop compensation so the service never issues an inverse write against an unknown state.

`move_page` validates one parent or space-root target. Same-space parent changes send a versioned
content update with the new ancestors; first/last/before/after ordering uses the isolated move action.
Cross-space moves use `position=append` for a parent or `position=topLevel` for a space root, then
poll the documented long-task route for at most 10 seconds and 40 times. A confirmed parent change
is retained when a separate same-space position step fails, and the result identifies that partial
state.

`delete_page` reads the direct children or full target subtree and checks `expand=operations` for
every affected page before its first write. `refuse` reports direct children; `move_up` re-parents
them in source order, then trashes the selected page; `trash` refuses more than 100 pages and
trashes descendants bottom-up. The complete subtree count is bounded by a 5,000-page read safety
cap. Former parent, sibling position and next sibling are saved on each page being trashed (the
selected page in `move_up`) in a content property, on a best-effort basis. An unreadable space
permission list adds a restore warning. Each
write stops at its first failure; results distinguish completed, unknown and unattempted pages, and
no completed write is automatically undone.

`list_trash` uses the documented content collection with `type=page&status=trashed`, batches of up
to 100, and stateless cursors bounded to offset 1,000,000. `restore_page` restores through the
documented status update, then tries caller placement, the saved property, and finally the space
root. Missing, invalid or unusable saved placement is logged and reported. If a same-space parent
change succeeds but its requested sibling position is definitively refused, the connector returns
the restored page to the space root; an uncertain recovery is reported for inspection. After a
successful restore, the connector removes its property when possible.

`export_pages` validates up to 500 references before any request, then reads two full pages at a time;
the transport still caps total in-flight HTTP requests at six. It resolves each requested version and
keeps the input order. Non-account page errors and attachment failures are written to
`_export-report.txt`; authentication and account mismatches abort the export. Text files use the same
unbudgeted `get_pages` renderer and always include the provenance header. Rich files start with that
header in an HTML comment, preserve the storage body, and append escaped comments when comments are
selected. Windows-invalid filename characters are replaced and duplicate page names receive suffixes.

One successful page with no attachment bytes requested and no incomplete sections is written as a
timestamped file. All other cases use a timestamped ZIP with page files at its root and attachment
folders beside their page files. `include_attachments` defaults to false; when true, the tool lists and
downloads all readable attachments for each page. `parse_scope.attachments` separately controls
whether attachment names appear in text. Exports and their staging files live in an account-specific
subfolder of `exports`; the shared local-file cleanup removes outputs older than seven days when the
folder is next used. After staging a page body and its selected attachments, the service retains only
page ID, title and version for archive naming so memory does not grow with the number of pages.
Export names fit within 180 characters and 240 filesystem bytes or UTF-16 code units.

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
