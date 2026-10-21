# Confluence Connector — Architecture

**Inputs:** [Requirements](requirements.md) · [Roadmap](roadmap.md) · [API research](api-research.md)

![Confluence connector architecture](diagrams/architecture.svg)

The classes and who calls whom, in more detail: [code map](diagrams/code-map.svg) (open it in a browser
for the tooltips). The diagrams give the overview; this document records the rules and contracts the code
and diagrams cannot show. *What* the connector must do is in the requirements; *why* each route was
chosen is in the research.

---

## 1. Principles

1. **Remote first.** Confluence does the searching, rendering and paging. The connector keeps no index,
   cache or copy of page content.
2. **No always-on service.** Every entry point is a short-lived local process, started on demand, for
   one user (§2).
3. **Thin surface, one service.** MCP tools validate arguments, call one service method and return a
   model. Domain decisions live in [`service/`](../src/confluence_connector/service/).
4. **Protocol knowledge stays at the edge.** Only [`remote/`](../src/confluence_connector/remote/)
   knows routes, Confluence JSON, paging and the private move action.
5. **Documented first, gaps filled.** Documented REST routes wherever they work; the private
   `movepage.action` and quick-navigation routes only for what REST cannot do (§6).
6. **Libraries for generic problems.** HTML parsing and Markdown conversion use BeautifulSoup and
   markdownify; the connector adds only Confluence-specific rules.
7. **Current format only.** No migrations or compatibility shims ([AGENTS.md](../AGENTS.md)).

## 2. Process model

There is no daemon. MCP runs only for its client session; `auth` and `status` are one-shot commands.

| Command | Lifetime | Does |
|---|---|---|
| `confluence-connector mcp` | One stdio process per MCP client session | Serves the 19 tools (§8) |
| `confluence-connector auth [--sign-out]` | One-shot | Prompts for a PAT, verifies and stores it, calibrates `cql_timezone`; or deletes the local token |
| `confluence-connector status [--check]` | One-shot | Offline account status; `--check` verifies the token and checks `cql_timezone` |

Every command accepts `--config PATH`. Exit codes: 0 success, 1 other errors, 2 sign-in required, 130
interrupted.

Consequences of short-lived processes:

- **No in-memory state outlives a call.** Cursors are self-contained (the normalized request plus the
  next offset or anchor) and survive a client restart.
- **No background work:** no schedulers, sync loops or warm-up. Calibration runs only inside `auth` and
  `status --check`, never on MCP startup or in a tool.
- **Concurrency is cross-process.** Several MCP processes may run at once; the token store locks across
  processes, and output files are claimed exclusively.
- **Startup is cheap.** Nothing is built before the first tool call. MCP never prompts; only `auth`
  asks for a token.

## 3. Layers

Surface → service → remote, as in the diagram. The import rules:

- [`surfaces/`](../src/confluence_connector/surfaces/) imports [`bootstrap.py`](../src/confluence_connector/bootstrap.py),
  [`domain/`](../src/confluence_connector/domain/) and the product names in
  [`config.py`](../src/confluence_connector/config.py) only. It never calls `remote/`.
- [`service/`](../src/confluence_connector/service/) imports `remote/` adapters, `domain/` and the
  shared helpers. It never sees Confluence JSON.
- [`remote/`](../src/confluence_connector/remote/) returns `domain/` models. The transport reads the
  token through the `CredentialSource` port ([`remote/ports.py`](../src/confluence_connector/remote/ports.py)).
- [`domain/`](../src/confluence_connector/domain/) imports nothing from the app.

## 4. Repository layout

The [code map](diagrams/code-map.svg) shows every package and class. Tests, with synthetic fixtures and
fake HTTP responses only, are under [`tests/`](../tests/); the research probe and the acceptance scripts
are under [`research/`](../research/README.md) and are never run as routine checks.

## 5. Module responsibilities

Only rules that the code layout does not make obvious are listed here. The per-tool contract is §8.

### 5.1 [`auth/store.py`](../src/confluence_connector/auth/store.py)

- `TokenStore` keeps one record, the PAT and its username, in `msal-extensions` encrypted persistence
  under `CrossPlatLock`. No plaintext fallback: if encryption is unavailable, every command fails with
  `SecureStorageUnavailable`.
- Explicit sign-out clears even malformed JSON without decoding it, under the same encrypted-store lock.
- `save` refuses a different username until `sign_out`. Every request loads the record again, so a
  sign-out or account switch takes effect on the next request in every process.

### 5.2 [`remote/transport.py`](../src/confluence_connector/remote/transport.py)

- One `httpx.AsyncClient` per process: HTTP/1.1, no redirects, six connections, 10 s connect and pool
  timeouts, 30 s read and write timeouts. A process-wide semaphore keeps **six requests in flight**
  across all tool calls. JSON responses are capped at 16 MiB; downloads stream to disk.
- **Reads** retry up to 3 attempts on connection errors, timeouts, 429, 500, 502, 503 and 504, honoring
  `Retry-After`, otherwise with short jittered backoff.
- **Writes** are resent only when they provably never applied: the request was never sent, or the answer
  was 429 or 503. A timeout after sending, an incomplete answer or a 504 raises `UncertainWrite` and is
  never resent.
- **Account checks.** The username is read before and after every exchange; if it changed, the result
  is discarded (`AccountMismatch`), and for a write the outcome becomes `UncertainWrite`.
- **401** raises `AuthenticationRequired` (run `auth`); **403** raises `PermissionDenied` for that page
  or operation, never a sign-in problem. Error messages carry the status and Confluence's message, never
  URLs or bodies.

### 5.3 [`remote/confluence.py`](../src/confluence_connector/remote/confluence.py)

- `ConfluenceApi` is the only code that knows routes and wire fields. Configured endpoint paths come from
  the private settings, relative to `base_url` and its context path.
- **Paging** advances on the returned `start + size`, never on the echoed `limit` or `content/search`'s
  `next` link, which skip results (research §5.2). Space listing reads 500 per request; children 500;
  versions 200, at most 5,000; drafts and trash 100.
- **Page reads** are three parallel requests: bodies (`view`, optional `storage`, version, history),
  `child/attachment?limit=500` and `child/comment?depth=all`. A requested body that is missing or
  malformed raises `Upstream`.
- **The private move action** is isolated in `position_page` and `move_page_action`: `POST
  /pages/movepage.action` with `pageId`, `spaceKey`, `position`, `targetId` and `mode`. Application
  errors in its JSON (`actionErrors`, `validationErrors`, `errorMessage`) raise. Cross-space moves use
  `ASYNC` and poll the documented `/rest/api/longtask/{id}` up to 40 times, at most 10 s; a task still
  running then is `UncertainWrite`.
- **Placement properties** (`get_page_property`, `set_page_property`, `remove_page_property`) store a
  page's former parent and position in a content property before it is trashed (research §6.4).

### 5.4 [`remote/links.py`](../src/confluence_connector/remote/links.py)

`PageLinkResolver.resolve` turns a numeric ID or a same-instance URL into a page ID and optional
version: `/spaces/KEY/pages/ID/…` and `viewpage.action?pageId=` directly; `/x/…` by following up to 5
redirects that must stay on the configured HTTPS origin and context path; `/display/KEY/Title` by an
exact-title CQL lookup in that space. Any other host or path is refused with `InvalidRequest`.

### 5.5 [`domain/`](../src/confluence_connector/domain/)

- Pydantic models are the only schema: FastMCP derives the tool schemas from them.
- Shared page-reference, space-key and current-page checks live in `validation.py`; CQL quoting and
  same-instance URL rules live in `cql.py` and `urls.py`.
- Errors: `AuthenticationRequired`, `AccountMismatch`, `SecureStorageUnavailable`, `InvalidRequest`,
  `NotFound`, `PermissionDenied`, `Throttled`, `Upstream`, `Conflict` and `UncertainWrite`. Each has a
  readable message; the surface returns it as a tool error.

### 5.6 [`service/`](../src/confluence_connector/service/)

Every input is validated once, at the service entry point, for every caller. Services are built per
account by `AppContext` ([`bootstrap.py`](../src/confluence_connector/bootstrap.py)).

Account-boundary error propagation is shared in `errors.py`. Attachment downloads and exports share
filename sanitization and filesystem component limits from `filenames.py`.

`placement.py` resolves parent, space, position and sibling inputs for page creation, movement and
restoration. Service entry points validate public references and provide their existing page resolver;
the shared resolver checks page state and sibling scope, and selects the sibling action target for
first and root-last placement. Move and restore allow missing status metadata; page creation requires
an explicit `current` status.

**[`search.py`](../src/confluence_connector/service/search.py)** builds CQL from literal terms and
validated scope only; terms escape text-query syntax separately from CQL string quoting. Title search
replaces punctuation with spaces and, on an unscoped first page, merges up to 20 quick-navigation
matches after the CQL hits, within `limit`. [`cql_dates.py`](../src/confluence_connector/service/cql_dates.py)
converts UTC bounds to CQL minutes in `cql_timezone` (requirements §7).

**[`timezone_calibration.py`](../src/confluence_connector/service/timezone_calibration.py)** takes the
newest readable page, reads its reference instant from REST metadata, and binary-searches
`lastmodified >=` minute cutoffs within ±15 hours to find
CQL's effective offset. The REST sample timestamp is rechecked as a UTC instant after measurement; a changed sample retries
once, then fails without a result to persist. The CLI calls it; `config.py` writes the result atomically
under a lock.

**[`tree.py`](../src/confluence_connector/service/tree.py)** reads space roots with `depth=root` and
each page's children in server order, stopping at depth 5 or 1,000 pages with `complete: false`.

**[`pages.py`](../src/confluence_connector/service/pages.py) and
[`page_text.py`](../src/confluence_connector/service/page_text.py)** convert a body in three steps:

1. Storage is parsed with BeautifulSoup for macro parameters (Jira keys, PlantUML source, titles,
   include targets) and typed references (`ri:page`, `ri:attachment`, inline-comment markers).
2. A BeautifulSoup pass over `view` replaces each `data-macro-name` wrapper with its marker, pairing
   wrappers with storage macros by name and document order (wrappers inside a resolved include take no
   local source); turns images and attachment links into markers with typed references; renders
   mentions and tasks; makes same-instance links absolute; and drops scripts and styles. Attachment
   identity is matched only after checking origin and source ownership; included content cannot inherit
   local attachment IDs by filename. Jira without a known storage key keeps its rendered text.
3. markdownify converts the result, with table handling (merged cells repeated; nested tables or
   code fall back to minimal HTML) and code fences longer than any backtick run in the source code.
   The text is split at blank lines outside matching fenced blocks, preserving fence-like code lines.

A `rich` read returns storage verbatim, split between top-level elements by the parser's source
positions so the slices join back exactly. Cursors carry page ID, version and a stable anchor; body
anchors follow the pinned block order, attachment and comment anchors their IDs.

**[`writes.py`](../src/confluence_connector/service/writes.py) and
[`write_markup.py`](../src/confluence_connector/service/write_markup.py)**: `to_storage` converts and
checks `rich` input with BeautifulSoup, converts code blocks and expand sections, and refuses active
content and excessive nesting (requirements §10.1). The HTML parser repairs some malformed input
before sending it to Confluence; the roadmap tracks whether that matches the D5 error contract.
`content_loss` compares inline-comment anchors, macro names and attachment references between the
stored and new bodies.

**[`structure.py`](../src/confluence_connector/service/structure.py)** runs moves, deletes and restores
as ordered steps that stop at the first failure and report every page's outcome; nothing is undone.

**[`localfiles.py`](../src/confluence_connector/service/localfiles.py)**: account-specific output
folders, cleanup of files older than seven days when a folder is next used (tolerating files another
process removed), exclusive name claims with ` (2)` suffixes, owner-only permissions on POSIX.

### 5.7 [`surfaces/mcp_main.py`](../src/confluence_connector/surfaces/mcp_main.py)

`build_server` declares the tools with their annotations and the server instructions; each tool calls
`await context.services()` and one service method, and logs its name, outcome and duration.
Annotations: reads are read-only and idempotent (`auth_status` also closed-world). Writes are never
read-only, and all but `create_page` and `restore_page` are marked destructive. `download_attachments`
and `export_pages` are marked destructive too: they write local files, and their cleanup removes expired
outputs.

### 5.8 [`config.py`](../src/confluence_connector/config.py) and [`logs.py`](../src/confluence_connector/logs.py)

- `load_config` selects the file (`--config`, `CONFLUENCE_CONFIG`, repository root), refuses missing or
  unknown keys before any prompt or request, and caches the result per process.
- [`logs.py`](../src/confluence_connector/logs.py) is the same module as in the `lrh-*` Microsoft 365
  connectors: one daily-rotated file per `mcp` process (the one-shot `auth` and `status` commands
  print to the terminal and write no log), kept seven days, with a filter that redacts bearer
  tokens and secrets. `httpx` and `httpcore`, which log URLs, are held at WARNING.

## 6. Capability routing

| Capability | Route | Documented |
|---|---|---|
| Token check | `GET /rest/api/user/current` | Yes |
| Spaces | `GET /rest/api/space?status=current|archived` | Yes; status values not listed |
| Content search, user search | `GET /rest/api/search` (CQL) | Yes |
| Title matches | `GET /rest/quicknav/1/search` | **No** (research §4.3) |
| Former users | `GET /rest/api/user?username=` | Yes |
| Trees | `…/space/{key}/content/page?depth=root`, `…/content/{id}/child/page` | Yes |
| Metadata and versions | `GET /rest/api/content/{id}`, `/rest/experimental/content/{id}/version` | Versions experimental |
| Page reads | `GET /rest/api/content/{id}`, `child/attachment`, `child/comment` | Yes |
| Attachment bytes | `GET /download/attachments/{id}/{file}` | Link from the attachment record |
| Create, update, publish, draft delete | `POST`, `PUT`, `DELETE /rest/api/content…` | Yes |
| Labels | `POST`, `DELETE …/label` | Yes |
| Same-space parent change | `PUT /rest/api/content/{id}` with `ancestors` | Yes |
| Positions, cross-space moves | `POST /pages/movepage.action`, then `/rest/api/longtask/{id}` | **No** (research §6.3) |
| Trash, restore, trash list | `DELETE …/content/{id}`, `PUT …?status=trashed`, `GET …?status=trashed` | Yes |
| Saved placement | `…/content/{id}/property` | Yes |
| Permission preflight | `expand=operations` | Yes |

To adapt to another instance, rerun the probes and acceptance checks
([research toolkit](../research/README.md)), record the results, and change the configured endpoints or
the adapter. The service and surface stay unchanged.

## 7. Data and identity

- **Account:** the username stored with the token. Services are rebuilt when it changes, and every
  response is checked against it (§5.2).
- **Page key:** the page ID, unique on the instance and stable across renames and moves.
- **Locations:** the data directory is `%USERPROFILE%\.lrh-confluence-connector` on Windows,
  `$XDG_DATA_HOME/lrh-confluence-connector` (or `~/.local/share/…`) elsewhere, or
  `CONFLUENCE_CONNECTOR_HOME`. It holds:
  - `downloads/<account>/`: attachments saved by `download_attachments`;
  - `exports/<account>/`: export files and their staging folders;
  - `logs/mcp-<start time>-<process id>.log`: one file per MCP process, rotated at midnight and kept
    seven days. `CONFLUENCE_CONNECTOR_LOG_LEVEL` sets the level (default INFO).

  - `token-cache.bin`: the encrypted token record (§5.1).

  Downloads and exports are removed after seven days.
- **Settings:** the private `confluence_config.json` (§5.8; requirements §6).

## 8. MCP tools

The server (`lrh-confluence-connector`) exposes 19 tools, grouped below by task. The
[README](../README.md#mcp-tools) lists them in a short table; this section gives each tool's inputs,
results and caveats.

### 8.1 Shared rules

- **Page references.** Every `page`, `pages`, `parent_id`, `sibling` and `draft` input takes a numeric
  ID or a same-instance URL (§5.4). A URL with `pageVersion` selects that version where a tool reads;
  write and structure tools refuse it.
- **Cursors.** Pass a returned cursor **alone**; it restores the original request. Cursors are
  self-contained, at most 16,384 characters, and offsets stop at 1,000,000.
- **Times.** Results carry the UTC timestamps REST returns. Date filters follow requirements §7.
- **Writes are explicit.** Only the write tools change Confluence, and only the pages they name. No tool
  purges the trash; `delete_draft` is the only permanent delete.
- **Partial results.** Multi-step tools report each step: done, `unknown` (uncertain: inspect before
  repeating) or `not_attempted`. Nothing completed is undone automatically.
- **Sign-in.** If a tool reports that sign-in is required, run `confluence-connector auth` in a terminal.
  An access-denied error concerns one page or operation, not the sign-in.

### 8.2 Status

#### `auth_status`

Offline: the configured instance, whether a token is stored, and its account name. It does not prove
that Confluence still accepts the token; `confluence-connector status --check` does.

### 8.3 Spaces, search and people

#### `list_spaces`

Every current and archived space visible to the account, with ID, key, name, type, status and link.
Read 500 at a time; `truncated` reports a cut.

#### `search_content`

Searches pages, blog posts, comments and attachments. `query` is literal text, up to 1,000 characters.
`match` is `all` (default), `any` or `phrase`; `fields` is `all` (default) or `title`. `limit` is 1–100
(default 25).

- `scope`: `space_keys`, `space_type`, `content_types`, `labels`, `creator_usernames`,
  `contributor_usernames`, `created_after`/`created_before`, `modified_after`/`modified_before`,
  `parent`, `ancestor` and `include_archived` (default true). Each list holds at most 50 values of at
  most 255 characters.
- Each hit has its ID, type, title, space, link, excerpt, modification time, parent and `sources`
  (`cql`, `quicknav` or both). `queries_sent` shows the CQL sent, and `approximate_total` is the
  server's estimate.
- Title search on an unscoped first page adds quick-navigation matches; scoped title searches skip
  them, because that route takes no filters.
- A comment hit is the comment, not its page.

#### `find_users`

Current users by display-name `query` (up to 255 characters), or one exact `username`, the only way to
find former users. Use one or the other. `limit` is 1–100 (default 25).

### 8.4 Trees and metadata

#### `get_page_tree`

Ordered children of exactly one `page` or `space_key`, to `max_depth` 1–5 (default 3), counting the
selected page as level 1 (for a space, its top-level pages). A page root also returns its ancestors.
At most 1,000 pages; `complete: false` and a note report a cut.

#### `get_page_metadata`

For 1–20 `pages`: title, status, space, link, current version, created and modified times and authors,
contributors, labels, ancestors, and the version list (at most 5,000 per page; `versions_truncated` marks
a cut).
Failures are listed per reference beside the successful pages. Versioned references are refused.

### 8.5 Reading pages

#### `get_pages`

Reads 1–20 `pages` as `text` (default) or `rich` (unchanged storage). `max_chars` is 1–400,000 (default
40,000). `parse_scope` turns on or off `header`, `macros_from_storage`, `attachments` and `comments`
(all on by default).

- Each page record has `text`, `references` (typed: kind, IDs, filename, media type, size, owner page),
  the attachment list and structured comments for the selected blocks, `version`, `total_versions`
  and `notes`.
- Whole pages are packed; pages that did not fit are in `remaining`. An oversized page returns a
  `next_cursor`; pass it alone to continue at the same version.
- Attachments are listed, never downloaded.

### 8.6 Local files

#### `download_attachments`

Saves 1–500 `attachment_ids` of one `page` to `downloads/<account>/`. The page's own attachment list is
read first, so an ID from another page is refused. Returns a path per saved file and a failure per
attachment that could not be saved; a partial file is removed.

#### `export_pages`

Writes 1–500 `pages` in full as `text` (default) or `rich`, and returns the path, size and counts.
`include_attachments` (default false) adds each page's attachment bytes. Pages are read two at a time.

- One page with nothing else is a single `.txt` or `.html`; otherwise one flat `.zip`: page files at
  the root, each page's attachments in a folder named after its file.
- File names: `<Title> (<page id>) v<version>`, within 180 characters and 240 bytes, with ` (2)`
  suffixes for duplicates. Every file starts with the provenance header and breadcrumb.
- Failures go to `_export-report.txt`; account and sign-in errors abort the export. Large exports take
  minutes.

### 8.7 Pages, drafts and labels

#### `create_page`

Creates a current page (default) or, with `status="draft"`, a new-page draft, under exactly one of
`parent_id` or `space_key`. `title` up to 255 characters; `body` up to 2,000,000 in `format` `rich`
(default) or `text` (requirements §10.1). `position` is `first`, `last` (default), `before` or `after`;
the last two need `sibling` under the same parent.

- A current page with the same title in the target space is refused, with its link, before anything is
  written.
- Position is a second write. If it fails, the page stays created, and the result's `warnings` says the
  position was not applied.

#### `update_page`

Replaces the whole body of a published page, and the title if given. `expected_version` is required:
a stale version is refused before writing, and a concurrent edit gets Confluence's 409. Neither is
retried. Inline-comment anchors that would be lost are refused unless `allow_comment_loss: true`;
removed macros and attachment references come back as `warnings`.

#### `list_drafts`

The account's new-page drafts, optionally for one `space_key`; `limit` 1–100 (default 50). Each draft has
an ID-based link that page tools accept.

#### `publish_draft`

Publishes a new-page draft with its full body, space and parent; the page keeps its ID.

#### `delete_draft`

Deletes a new-page draft **permanently**: it bypasses the trash and cannot be restored.

#### `set_labels`

Adds and removes global labels on one published page, one request per change, in order, stopping at the
first failure. Returns `added`, `removed`, `unchanged`, `failures` and `not_attempted`. Successful changes
are never reversed; inspect an uncertain outcome before retrying.

### 8.8 Moves, trash and restore

#### `move_page`

Moves one current page under exactly one of `parent_id` or `space_key` (the root), with the same
`position` and `sibling` as `create_page`.

- Within a space: one `movepage.action` request applies both the parent and position, so the move does
  not create a page version. A definite refusal reports the previous placement; an uncertain response
  reports an unknown placement and tells the caller to inspect the page before retrying. Root `last`
  placement uses `below` the last root page because `topLevel` places a page first.
- Across spaces: one move action, waited on through the long-task route for at most 10 seconds. If
  Confluence refuses the move, recreating the page in the target space and trashing the original is a
  separate plan that loses the page ID, history and comments.

D7 requires one move action without creating a page version. The same action also handles `move_up`
and restore placement; see the [implementation item](roadmap.md#one-route-for-same-space-moves).

#### `delete_page`

Moves a page to the trash. `children` is `refuse` (default), `move_up` or `trash` (requirements §10.5).

- The whole affected hierarchy is read first (at most 5,000 pages) and `delete` permission is checked
  on every page before any write. `trash` refuses more than 100 pages, with the count.
- Each trashed page's parent and position are saved first, best effort, for `restore_page`.
- `move_up` rechecks each child’s parent and space immediately before its move, skipping changes.
  Children moved under a parent append there in their original order; root moves append below the last
  root page. An empty ancestor list is never used to move to the root.
- The result lists every affected page with its former parent and position and its status
  (`trashed`, `moved_up`, `skipped`, `failed`, `unknown` or `not_attempted`), `complete`, and a warning when restoring will
  likely need a space administrator.

#### `list_trash`

Trashed pages of one `space_key` or every visible space: ID, title, space and last edit. `limit` 1–100
(default 50). Confluence exposes no deletion time or author.

#### `restore_page`

Restores a trashed page, then places it: under the caller's `parent_id` or `space_key` (with
`position` and `sibling`) when given, otherwise where the saved property says, otherwise at the space
root. `placement_source` (`caller`, `saved_property` or `space_root`) and `placement_status` say which
applied and whether it worked; a refused placement leaves the page at the root with a warning.

## 9. Cross-cutting concerns

| Concern | Rule |
|---|---|
| Logging | Tool name, outcome, duration and counts. Never page content, queries, request URLs or tokens. |
| Retries | Reads: 3 attempts on connection errors, timeouts, 429 and 5xx, honoring `Retry-After`. Writes: only when never sent or answered 429/503. |
| Concurrency | Six requests in flight per process; page reads in parallel parts; exports two pages at a time. |
| Bounds | Every list and read is bounded with a cursor or a cap reported in the result. Attachment bytes go to files, never into tool results. |
| Errors | Domain errors with Confluence's status and message. 401 → `AuthenticationRequired`; 403 → `PermissionDenied`; changed account → `AccountMismatch`; uncertain write → `UncertainWrite`. |
| Security | Configured host only; no arbitrary URL fetching; short links followed only on the configured origin; no browser cookies; tokens never in tool inputs or results. |
