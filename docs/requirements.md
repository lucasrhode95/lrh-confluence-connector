# Confluence Connector — Requirements

**Evidence:** [Confluence API research](api-research.md) · **Tool schemas:** [MCP declarations](../src/confluence_connector/surfaces/mcp_main.py) · **Open work:** [roadmap](roadmap.md)

This document records what the connector must do and the decisions behind it, D1–D10, decided
2026-10-19 unless dated otherwise. Tool input/output schemas are published by MCP; open decisions
are in the roadmap. Do not resolve an open decision by assumption.

---

## 1. Product

A local MCP server for **one user's account on one Confluence Data Center instance**. Agents search,
read, export and edit that user's wiki content through it. Several MCP processes may run at once, one
per agent session.

There is no hosted service, web UI, scheduler or background crawler. Unlike the Microsoft 365 services
the other `lrh-*` connectors use, a Data Center instance is organization-hosted: its URL, context path
and routes are deployment-specific and live in a required private configuration file (§6), never in
code.

**Guiding principle: documented first.** Prefer documented Confluence REST routes. Use an undocumented
browser route only for a required capability that has no documented path, isolated in one place, with
its failures reported and no silent fallback (D7, §10.4). A route seen in UI source is a lead, not
proof that it works.

## 2. Ownership (2026-10-18)

- The connector owns Confluence spaces, pages, drafts, trees, labels, comments, attachment references
  and attachment retrieval.
- Agents own interpretation: multilingual query formulation, judging documentation quality, and
  cross-checking against other connectors or source code. The agent, not the connector, decides how to
  reconcile conflicting sources.
- Links to other products stay links. A genuinely embedded component of another product needs its
  source and owner investigated first.
- The wiki holds wrong, duplicated and outdated pages. Results keep provenance and metadata, and never
  present a search hit or a recent edit as proof of authority.

Confluence credentials are unrelated to the shared Microsoft sign-in (`lrh-m365-auth`); only the
storage technique is shared (§5).

## 3. Use cases

| # | Use case | Capabilities used |
|---|---|---|
| A1 | "How does our release process work end to end?" The agent searches with several phrasings, reads the main pages and their children, then checks the code or tickets elsewhere. | `search_content` (repeated) → `get_pages` → `get_page_tree` |
| A2 | "Read the onboarding subtree and propose a clearer handoff with fewer duplicates." | `get_page_tree` → `get_pages` / `export_pages` |
| A3 | "Explain the context of this Teams thread." The agent reads Teams, then looks up the wiki pages it mentions. | Teams MCP + `search_content` → `get_pages`; **no cross-repo integration** |
| A4 | "Find the page Alex wrote about the migration around March." | `find_users` → `search_content` with `creator_usernames` and `created_after`/`before` |
| A5 | "Who maintains this page, and what changed recently?" | `get_page_metadata` |
| A6 | "Draft a runbook page under Operations; I will review it before publishing." | `create_page(status="draft")` → `list_drafts` → `publish_draft` |
| A7 | "Fix the outdated port numbers on this page." | `get_pages(format="rich")` → `update_page(expected_version=…)` |
| A8 | "Move these pages under the new Platform parent and trash the obsolete one." | `move_page`, `delete_page`, later `restore_page` if needed |

## 4. Non-goals

- A local search index, ranking or content cache. Live search found known pages reliably (research
  §4); revisit only on evidence of gaps.
- Raw CQL from agents: search takes literal text and structured filters.
- Purging the trash, ever. `delete_draft` is the only permanent delete, because Confluence offers no
  trash for drafts.
- Unpublished edits of published pages: the editor-draft route cannot be read back and belongs to the
  non-collaborative editor (research §6.2). Agents propose changes as new-page drafts or edit with
  `expected_version`.
- Comment writes.
- Interpreting attachment files (parsing, OCR, transcription). Agents receive the raw file.
- Version comparisons, activity feeds and automated documentation-quality analysis (follow-ups).

## 5. Authentication (D1)

- A user-generated personal access token (PAT); no administrator registration or assistance. Native
  OAuth needs an administrator-registered client and is not used (research §2.2).
- `confluence-connector auth` prompts for the token without echoing it, verifies it with the configured
  current-user endpoint, and only then stores it with the returned username and canonical instance URL
  (including context path). Tokens never pass through MCP tools.
- Storage is `msal-extensions` encrypted persistence under its cross-process lock, as in
  `lrh-m365-auth`, with no plaintext fallback: if encryption is unavailable, the connector refuses to
  run. The token is read for every request, not kept in process memory.
- A different username or instance is refused until `auth --sign-out`, so switching is explicit.
  Unbound or malformed encrypted records fail closed; they are not migrated automatically.
  `--sign-out` warns that the token cannot be recovered, then deletes it locally; it does not revoke it
  in Confluence.
- `status` reports the stored account offline; `status --check` verifies it. `mcp` runs the stdio
  server. The command set matches the Microsoft 365 connectors.
- If the stored account changes after a write's response arrives, the write is reported as uncertain
  and nothing more is sent. No result ever crosses an account boundary.

## 6. Configuration

- One required private JSON file, shared by the app and the research probe and loaded by one loader.
  Selection: `--config PATH`, then `CONFLUENCE_CONFIG`, then `confluence_config.json` at the repository
  root. No instance, route or query is embedded in code. Settings are cached per process: restart the
  MCP client after editing the file.
- `cql_timezone` (optional): an IANA zone or a fixed `UTC±HH:MM`. `auth` and `status --check` measure
  CQL's effective offset with bounded read-only searches; an unset value is written as the measured
  fixed offset, atomically, keeping the other settings. An existing value is only compared, by
  `status --check`, with a warning on mismatch. A fixed offset does not follow daylight saving; replace
  it with an IANA zone where that applies.
- `search_response_max_chars` (optional): JSON-character budget for one `search_content` response,
  from 1,000 to 1,000,000; default 40,000. Oversized pages are reduced and continued by cursor.
- `acceptance` (optional, research only): `personal_space_key` and `cross_space_key`, the two spaces the
  acceptance scripts may write to. A missing key skips only the checks that need it; the scripts never
  pick a space themselves.

## 7. Search and navigation (D2)

- **Scope** is a structured object: spaces, space type, content types, labels, creators, contributors,
  created and modified ranges, parent, ancestor, and `include_archived`. Archived and personal spaces
  are included by default. Unsupported fields are refused, never dropped.
- **Matching:** `match` is `all` (default: one clause per term, joined by AND), `any` or `phrase`.
  Terms split on whitespace only; punctuation inside a term is kept (code identifiers such as `x_y.z`
  are indexed whole) and punctuation at its edges is dropped. Query text is always literal: operators,
  quotes and wildcards are never interpreted.
- **Queryless listing:** omit `query` only when scope includes at least one contributor, creator,
  created/modified date bound, space key, label, parent or ancestor filter. This avoids an unbounded
  listing. If `content_types` is omitted, a queryless search lists pages. `contributor` means anyone
  who edited the content; `creator` means its original author. Resolve display names with `find_users`.
- **Ordering:** `relevance` is the default for text searches; queryless searches default to
  `modified_desc`. `modified_asc` and `created_desc` are also supported. Cursor state retains the
  selected order and detail mode. Standard CQL documents `ORDER BY`; use with this instance's
  observed `siteSearch` clause remains a live verification item (roadmap).
- **Fields:** `all` (titles, bodies, comments, attachments) or `title`. Title search replaces
  punctuation with spaces, which found titles that quoting and escaping missed (research §4.3), and
  merges results from the private quick-navigation route, labelled by source; a quick-navigation
  failure is reported without failing the call.
- **Results** report the query sent, a self-contained cursor built from the returned `start + size`,
  and the server's total, labelled approximate. `detail="full"` preserves the existing hit shape;
  `detail="compact"` returns only content ID/type, title, space key, URL, modified time, latest
  editor, and parent ID. Every hit includes `last_modified_by` when the expanded latest version
  provides it, otherwise null. A configured JSON-character budget (`search_response_max_chars`,
  default 40,000) reduces server page size and returns a cursor so remaining hits are not skipped.
  A single full-detail hit that exceeds the budget requires compact mode or a larger configured budget.
- **Unsearchable terms:** punctuation-only and wildcard-only queries explain that wildcards are not
  query operators and point to scoped listing. A small common-English stopword list adds a cautious
  note; Confluence's actual analyzer depends on instance version and indexing-language configuration.
- **Dates:** a date-only value (`2026-03-14`) means that whole day in Confluence's time zone and needs
  no calibration. A timestamp with an offset is an instant; one without is UTC. Timestamps become
  zone-less CQL minutes in `cql_timezone`, rounding lower bounds down and upper bounds up so minutes
  stay inclusive. A repeated daylight-saving hour is widened to include both occurrences; a minute in a
  spring-forward gap moves to the next valid minute. Cursors keep the original UTC instants. Without
  `cql_timezone`, timestamp bounds are refused with instructions, never guessed.
- **Language:** stemming, stop words and accent folding follow the instance's indexing language,
  which the connector cannot change. The tool description states the observed rules so agents retry
  with variants, such as Portuguese plurals.
- **References:** tools accept page IDs and every same-instance page URL form (D7, §10.4). Trees are
  read in server order with bounded depth, counted as levels below the root for a page or a space
  alike (1 lists direct children or top-level pages; 1–5, default 3); users are found by display name,
  or by exact username for former users.

## 8. Reading and parsing (D3, D4)

### 8.1 Text form (D3, revised 2026-10-21)

- Confluence has no plain-text output (research §5.1), so the connector converts the `view` body,
  where includes, excerpts and expand sections are already resolved. Jira keys, PlantUML source, macro
  parameters and the HTML macro's identity come from `storage`, because `view` loses them.
- The goal is faithful, readable text for agents, not exact Markdown. Generic HTML conversion is
  delegated to markdownify; the connector adds only Confluence-specific handling: macro markers,
  attachment and image markers, typed references, link normalization to the configured instance, and
  the table rule below. Library output is accepted where these rules are silent.
- Markers sit where the item appears: `[Image: filename — alt text]`, `[File: filename]`,
  `[Included from: title]`, `[Jira: KEY]`, `[Diagram: plantuml]` followed by its source,
  `[Code: title]` before titled code, `[Expand: title]`, `[Status: text]`, panels as quoted blocks
  (`> [Info] …`), `@Display Name`, tasks as `[ ]`/`[x]`, and `[Unrendered macro: name]` for anything
  else without text. Code blocks are fenced with their language. Scripts, styles and tables of contents
  are dropped. The HTML macro has no wrapper in `view`, so only its scripts are dropped and an
  `[Unrendered macro: html]` marker follows the body.
- Ordinary tables use the library's pipe tables. Merged cells, nested tables and tables containing
  code blocks retain minimal ordinary HTML, including spans, link destinations and code whitespace.
  Agents see this markup directly in the text response; it is not Confluence storage markup.
- Accepted limitations: exotic nesting (lists or panels in table cells) follows the library, and list
  numbering, spacing and indentation may differ from Confluence's display.
- Layout: a short header (title, link, version and total versions, last modified, extracted at), the
  body, `## Attachments` (type and size), then `## Comments`, threaded, with author, date, location,
  resolution and an inline comment's selected text.
- A typed `references` list (kind, IDs, filename, media type, size, owner page) is collected from
  `view` attributes and `storage` elements **before** conversion. Agents never parse marker text, and
  the connector never reads identity back from it.
- A requested body that is missing or malformed is an upstream error, never an empty page.

### 8.2 Budgets and continuation (D4)

- `get_pages` takes up to 20 references and `max_chars` (default 40,000, at most 400,000), and returns
  pages in the order given. Whole pages are packed into the budget; the first is always returned, and
  pages that did not fit are listed in `remaining`.
- A larger page is cut between blocks: paragraphs, whole lists, tables and code blocks in text; top-level
  storage elements in `rich`. A single block over the budget is returned whole with a note. Comment
  bodies share the budget.
- The cursor carries page ID, version and the next block's anchor. A continuation reads that same
  version, through a historical read if the page changed meanwhile; attachment and comment sections
  resume by item ID. A removed anchor asks the caller to restart. Joined fragments reproduce the
  original text exactly. Nothing is kept in process memory.
- `parse_scope` defaults: `comments` (footer and inline, resolved included), `attachments` (the list
  only), `macros_from_storage` and `header`, all on. Reads never download attachment bytes.
- A reference with `pageVersion` reads that version. Metadata always describes the current page.

## 9. Exports (D8)

- `export_pages` writes up to 500 pages in full, with no budget, and refuses larger requests before
  starting. It warns that large exports take minutes.
- Text exports keep the existing layout: one successful page without attachment bytes is a single
  file; other text exports are one `.zip`. Rich exports of up to 20 pages without attachment bytes
  are separate local files; larger rich exports and every export with attachment bytes are one ZIP.
  Failures in a direct rich export produce a separate report file.
- Each page body file is at the root named `<Title> (<page id>) v<version>`, with characters Windows
  forbids replaced and long titles shortened to fit 180 characters and 240 filesystem bytes. Its
  separate comments file uses the `.comments.html` suffix. No tree folders; each body header carries
  the breadcrumb (`Space › Parent › Page`), always, even when `parse_scope.header` is false.
- `text` files use the unbudgeted `get_pages` text. Each rich page has an editable body `.html` file
  containing the unchanged storage body and the header facts with a `Page ID:` line in a leading
  comment. When comments are in scope, they are written to a separate `<Title> (<page id>)
  v<version>.comments.html` file. That comments file is read-only and has no body header.
- The result lists output files with a stable role (`body`, `comments`, `attachments`, `report` or
  `archive`) and a local path. A ZIP result is listed as an `archive`; its members retain their page
  names and comments use the `.comments.html` suffix.
- Attachment bytes are excluded by default; with `include_attachments`, they go in a folder named after
  the page file. `parse_scope.attachments` separately controls the list in the text.
- A report file (or `_export-report.txt` inside a ZIP), present only when something failed or was
  skipped, names each item and why. Authentication and account failures abort the export.
- Output goes to the per-user exports folder. File tools are annotated as changing the local
  environment, and their files are owner-only on POSIX.

## 10. Writes

Daily use needs page and draft create, read, update and delete, labels, moves, and trash and restore.
Writes run only when a write tool is called, and are tested only on disposable synthetic pages.

### 10.1 Body formats (D5)

- `create_page` and `update_page` take exactly one of `body` and `body_file`, plus `format`. `rich`
  (default) is HTML plus Confluence storage tags, written as storage. `text` is escaped and split into
  paragraphs at blank lines. No wiki markup or Markdown input. `body_file` is an absolute path to a
  UTF-8 `rich` storage file; a leading byte-order mark is ignored. The resolved file must be inside the
  connector's exports folder or a folder the MCP client shares as a root, so a request cannot copy
  other local files into a page.
- Before sending `rich`, three conversions fix HTML that AI tools write and Confluence stores badly
  (research §6.5): `<pre><code class="language-X">` and plain `<pre>` become the code macro;
  HTML5 void elements are self-closed; `<details>`/`<summary>` become the expand macro. A
  `body_file` is sent byte for byte except new `<pre>` and `<details>` blocks, which get the same
  conversions; a `<pre>` already in the stored body (Confluence's preformatted style) is kept, and void
  elements are not rewritten, so unparseable markup returns Confluence's 400.
- Refused before sending: `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `on*` attributes,
  `javascript:` URLs, the HTML macro, and more than 128 nested elements. Inline `body` markup is
  currently reparsed with BeautifulSoup, which repairs some malformed input before sending (roadmap §7
  tracks returning Confluence's error instead); a `body_file` is sent as is, so its unparseable markup
  returns Confluence's 400 with row and column.
- Write tool descriptions carry a short storage reference (code, panels, expand, status, table of
  contents, tasks, page and attachment links, images, mentions).
- `get_pages(format="rich")` returns storage unchanged for editing, like Outlook's `body="html"`.

### 10.2 Edits (D6)

`update_page` always replaces the whole body (and optionally the title), as edits do in the Teams and
Outlook connectors; there are no section, append or find-and-replace modes.

- `expected_version` is required with `body`. A stale version is refused before writing, and a
  concurrent edit is refused by Confluence's 409; neither is retried.
- File-based edits (approved 2026-10-28): export the page with `export_pages` (`format: "rich"`),
  edit the file listed with the `body` role, and pass it as `body_file` with the same `page`. The caller always
  names the target: the header is editable text, so its page ID must match `page` but never selects
  it. The header's version is the expected version; an explicit `expected_version` must match it.
  Files without the header, text exports, files from another page, files with an inline comments
  section and the separate comments file are refused. Line endings are kept as they are in the file. An unedited file
  leaves the stored body byte-identical. `create_page` also accepts a plain storage file; a rich
  export's header is dropped there and its storage kept as is. Export files are removed seven days
  after their last change, so an edit that takes longer must export the page again.
- The stored and new bodies are compared first. Dropping inline-comment anchors detaches those comments,
  so it is refused unless `allow_comment_loss: true`; removed macros and attachment references are
  reported as warnings.

### 10.3 Page, draft and label tools (D10)

- `create_page` takes exactly one of `parent_id` or `space_key`, creates a current page or a new-page
  draft, and refuses a current page with the same title in the target space before creating, with its
  link.
- Drafts are new-page drafts only. `list_drafts` pages with a self-contained cursor; `publish_draft`
  keeps the page ID; `delete_draft` is permanent and bypasses the trash.
- `set_labels` (revised 2026-10-20) takes add and remove lists of global labels, skips changes already
  in place, and sends one request per change in order, stopping at the first failure. It never sends
  an inverse request: Confluence does not say whether this call caused a label to change, so undoing
  could remove another client's label. Results list what was added, removed, unchanged, failed and not
  attempted.
- Bounds: titles of 255 printable characters; labels and space keys of 255 ASCII letters, digits,
  underscores or hyphens (space keys may start with `~`); inline bodies of 2,000,000 characters.
  `body_file` has no connector size limit.

### 10.4 Routes, identity and placement (D7)

- Avoid routes and fields known to be deprecated, documented or not. Experimental routes, such as the
  version list, are flagged in the research record.
- Pages are identified by ID, which survives renames and moves; titles are unique only within a space.
  Tools resolve every reference to an ID first: `/spaces/KEY/pages/ID/…`, `viewpage.action?pageId=`
  and the editor's `editpage.action?pageId=` carry it; `/x/…` short links are followed; `/display/KEY/Title` is looked up by title and fails
  clearly after a rename. `pageVersion` selects a version; anchors are ignored. Other hosts are
  refused.
- Placement is one `parent_id` (the space follows from it) or `space_key` alone for the root, plus an
  optional `position`: `first`, `last` (default), or `before`/`after` a `sibling` under the same parent.
  A `space_key` that disagrees with the parent fails the call.
- Same-space moves use the private `movepage.action` for both parent changes and sibling positioning,
  in one request, without creating a page version. A move alone must not create a content version.
  This deliberately makes the verified private action required for all same-space moves; failures are
  reported without falling back to a version-creating content update. After page creation, a refused
  positioning action leaves the created page in place and reports that positioning was not applied.
- Cross-space moves use `movepage.action` with the target space and wait on the documented long-task
  route; ID, version, children, attachments, comments and labels are kept (research §6.3). If
  Confluence refuses one, the tool description tells agents that recreating the page in the target
  space and trashing the original loses its ID, history and comments.

### 10.5 Delete and restore (D9)

Confluence restores every page at the space root, whether deleted through REST or the UI, and its
children become top-level pages when it is trashed (research §6.4). The connector compensates:

- `delete_page` moves pages to the trash, never purges, and takes `children`: `refuse` (default; fails
  and lists the children), `move_up` (reparents the direct children under the page's parent or the
  space root, in order, then trashes the page) or `trash` (the whole subtree, bottom-up; more than 100
  pages is refused with the count).
- Before any write it reads the whole affected hierarchy (stopping at 5,000 pages without writing) and
  checks the `delete` operation on every page. When the space's permissions cannot be read, it warns
  that restoring will probably need a space administrator.
- Before trashing, it saves each page's parent and position in a content property, best effort; a
  failure to save is logged and does not stop the delete.
- `restore_page` restores, then places the page: the caller's placement first, otherwise the saved
  property, otherwise the space root. Any failure there is reported and leaves the page at the root;
  the property is removed after a successful restore.
- `list_trash` lists trashed pages for one space or every visible space (title, space, last edit, ID).
  Confluence exposes no deletion time or author.
- Results list every affected page with its former parent and position, and whether its step was done,
  uncertain or not attempted. A failed step stops the operation; nothing is undone automatically.

Delete outcomes distinguish confirmed `trashed`/`moved_up`, a concurrent parent/space change (`skipped`),
a definite operation failure (`failed`), uncertain application (`unknown`), and later steps
(`not_attempted`). A child moved elsewhere after preflight is not moved back by `move_up`.

### 10.6 Attachment management

- `upload_attachments` takes one current page and one to 100 absolute local file paths. New filenames
  create attachments; an existing filename uploads bytes through
  `/content/{page}/child/attachment/{attachment}/data` as a new version. The instance enforces its
  size limit. The connector preflights readable files and duplicate names, infers MIME type from the
  extension, and returns IDs, versions, media types and download URLs per file.
- `update_attachment` changes the name, version comment or MIME type without replacing bytes. IDs
  must belong to the named page; renames cannot collide with another current filename.
- `trash_attachments` never purges. It refuses current body references unless `allow_referenced` is
  explicit and checks the attachment's delete operation separately from page update access.
  `restore_attachments` verifies the original page. Results contain per-item successes and failures;
  writes already completed remain in place and uncertain outcomes stop later writes. References from
  other pages are not searched and are called out in the result.
- Inline images, including SVG, use an `ac:image` / `ri:attachment` reference. The `view-file` macro
  shows SVG as a file icon, and raw inline SVG is stripped by Confluence.

### 10.7 Watcher notifications

- Every write tool accepts `notify_watchers`, default `true`. For writes that create a content version,
  `false` sends `minorEdit: true`; `true` omits the field and keeps Confluence's default behavior.
  This applies to page edits, draft publishing, page restore and attachment versions, including
  multipart uploads. The result echoes the requested value; Confluence reads `minorEdit` back as
  `false`, so it cannot confirm which value was sent.
- Page creation, draft deletion, label changes, moves and trash routes have no verified suppression
  field. Their tool descriptions and results report that limitation and the server's default behavior.
  A page restore followed by a placement move reports the same limitation when the move is applied.

## 11. Reliability

- Report inaccessible pages, unsupported elements, failed downloads and partial coverage explicitly.
  Keep error messages short and readable, and keep page content out of logs.
- **Transport** (from the concurrency measurements, research §3): one lazily built async `httpx`
  client per process, kept across tool calls, HTTP/1.1, at most six connections, and **one limit of six
  requests in flight for the whole process**, shared by every tool call. Processes do not coordinate;
  a few concurrent sessions stay within the ten-parallel range tested without throttling.
- Each page read is up to three parallel requests (bodies with version and history; attachments;
  comments with replies), skipping parts the parse scope turns off. Embedded `children.*` expansions
  are not used: they stop at 25 and omit replies.
- Timeouts: 10 s to connect or wait for a pooled connection, 30 s to read or write.
- Reads retry up to three attempts on connection errors, timeouts, 429, 500, 502, 503 and 504, honoring
  `Retry-After` up to 30 s; a longer `Retry-After` is reported at once. A write is retried only when it
  provably never applied (not sent, or answered 429 or
  503); a timeout or any other uncertain outcome is reported for inspection, never resent.
- Slow operations are described in tool descriptions so agents choose them knowingly; a broad query
  needs no extra confirmation.

## 12. Acceptance

Functionality is tested against synthetic fixtures with known facts, never against real wiki content or
its ranking.

- A setup script creates a `Connector Lab Acceptance` tree in the configured personal space: unique
  markers, a punctuated title, a code identifier, accented words, every macro type, merged and nested
  tables, an image and an attachment, a footer comment with a reply, a label, ordered children with a
  grandchild, and several versions. One inline comment is added by hand, because only the UI creates
  one.
- Setup writes only with `--apply`; the runner sends requests only with `--run-live`, and writes only
  with `--allow-writes`. Missing manual steps or settings make only the dependent checks `skipped`.
- Checks cover search modes and filters (tolerating index delay), paging, link and version resolution,
  page text per D3 and D4, tree order, metadata, export layout per D8, and every write path, including
  conflicts, refusals, the content-loss check, cross-space moves and the three `delete_page` modes.
- Every live check also has offline synthetic coverage, run before every commit. The live runner reports
  pass, fail or skipped with timings and no page content.
- **Ready for daily use** means the offline suite passes and every live check passes or is skipped for
  a stated reason.
