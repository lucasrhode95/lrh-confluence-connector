# Confluence connector requirements

Decisions recorded 2026-10-18. These describe the intended product, not completed implementation.
See [architecture](architecture.md) for the current proof of concept.

## 1. Product and ownership

A local MCP connector for one user's account on one Confluence Data Center instance. Multiple MCP
processes may run concurrently. No hosted service, web UI, scheduler or background crawler is requested.

The local connector accesses an organization-hosted Confluence deployment. Unlike the Microsoft 365
SaaS services used by the other connectors, its server URL, installation context and endpoint settings
are deployment-specific. Keep them in explicit private configuration rather than assuming a shared
vendor service address. This is the reason for the required configuration file.

The connector owns Confluence pages, drafts, trees, comments, attachment references and retrieval.
Agents own interpretation, multilingual query formulation, assessing documentation quality and
cross-checking evidence against other connectors or source code. External product links stay links;
handling a genuinely embedded component requires investigation of its source and ownership.

Prefer documented APIs. When they cannot provide a required capability, investigate undocumented
browser/plugin methods. Source presence alone is not proof of a usable endpoint.

## 2. Agent use cases

- Research an end-to-end operational process across Confluence and other products.
- Read a documentation subtree and propose a clearer operational handoff with fewer duplicates.
- Use documentation to explain the context of a conversation from another connector.
- Find a topic using a remembered contributor and date range rather than a known page title.
- Find, explain, summarize and compare pages; identify documented decisions and maintainers.

The wiki can contain wrong, incomplete, duplicated and outdated material. Retrieval must preserve
provenance and available metadata without treating a search hit or recent edit as proof of authority.

## 3. Search and navigation

Search by page title and textual content, across all accessible spaces or selected spaces. Include
archived content and personal spaces by default. Use a structured `scope` object for space, title,
labels, author/creator, contributor, creation/modification dates and parent/ancestor filters; do not
require agents to compose raw CQL. Preserve distinctions between filters that the server treats
differently. Matching and paging follow D2 below; the exact `scope` field names are set when the
tool is built.

Do not silently interpret ordinary query words as boolean operators or drop unsupported scope fields.
Attachment filenames and server-provided alt text are desirable searchable context where available.

**Decided 2026-10-19 (D2), query semantics:**

- `match` selects `all` (default: every term required, one `siteSearch` clause per term joined with
  AND), `any` (terms sent together, which the server treats as any-term) or `phrase` (terms in order).
- Terms split on whitespace only. Punctuation inside a term is kept, because code identifiers such as
  `x_y.z` are indexed whole. Punctuation standing alone or at a term's edges is dropped. Query text is
  always literal: operators, quotes and wildcards are never interpreted.
- `fields` selects `all` (titles, bodies, comments, attachments) or `title`. Title search replaces title
  punctuation with spaces, which tested more reliably than CQL escaping, and merges `title ~` results
  with quick navigation (undocumented). Results are de-duplicated and labelled by source; a quick
  navigation failure is reported without failing the call.
- Results report the query actually sent, an opaque cursor built from `start + size`, and the server's
  total, labelled approximate.
- Stemming, stop words and accent folding come from the instance's administrator-set indexing language
  (English here). The connector cannot control them. The tool description states the observed rules so
  agents retry with variants, such as Portuguese plurals.

Support direct page references and topic discovery, complete page reads, and page-tree navigation.
Live search found known pages reliably in the 2026-10-19 probes, so no local index, ranking or cache
is planned. Revisit only if later evidence shows gaps. Exhaustive-search behavior across very large
result sets remains open.

## 4. Content reading and parsing

Support plain text and Confluence's enriched representation for reads and writes: D3 below sets the
text form, and D5 (§6) sets `rich` (HTML plus Confluence storage tags) as the enriched form.

Text extraction preserves headings, tables, code blocks, lists, panels and other textual structure.
Investigate resolving textual macros/includes or authoritative server-provided alternative text.
Report unresolved content explicitly. Do not emit binary bodies or base64 strings into agent text.

A structured parsing/fetching scope controls optional content. Include page comments, inline comments
and resolved discussions by default. Place inline attachment/image markers at their source positions;
append other attachment references after the body and before comments. Include filenames and useful
source-provided descriptions. Markers let an agent request a particular attachment later.

**Decided 2026-10-19 (D3), text and markers:**

- Confluence renders macros but offers no plain-text output, so the connector converts HTML to text.
  It reads the `view` body, where includes, excerpts and expand sections are already resolved, and
  takes Jira keys, PlantUML source and HTML-macro identity from `storage`, because `view` loses them.
- Output is Markdown-like: headings, lists, task lists, fenced code with language, panels as quoted
  blocks (`> [Info] …`), `[Expand: title]`, `[Status: text]`, `@Display Name`, and links with page IDs
  kept in the references. Tables of contents and generated numbering are dropped.
- Tables are pipe tables. Multi-paragraph cells are joined with `<br>`, list items become `• item`,
  and images and macros in cells use their normal markers. Merged cells repeat their value into each
  covered cell, with a `[Table: merged cells repeated]` note. Only tables with nested tables or code
  blocks in cells fall back to minimal HTML (`table/tr/th/td`, `colspan`, `rowspan`, text).
- Markers sit where the item appears in the body: `[Image: filename — alt text]`, `[File: filename]`,
  `[Included from: title]`, `[Jira: KEY]`, `[Diagram: plantuml]` followed by its source, and
  `[Unrendered macro: name]`. HTML-macro scripts are not emitted.
- Layout: a short header (title, link, version and total versions, last modified, extracted at), the
  body, `## Attachments` (all attachments with type and size), then `## Comments`, threaded with
  author, date, location, resolution and an inline comment's selected text. There are no in-body
  comment anchors for now. Search returns comment hits as comment results; the connector does not
  map them to their page.
- A typed `references` list accompanies the text (kind, IDs, filename, media type, size, owner), so
  agents never parse marker text.

Extract metadata and ordered content references from source fields/structure before rendering. Carry
them through typed models; never recover attachment identity or ownership from exported text markers.

## 5. Metadata, versions and exports

Default page reads show the latest published version. Include concise provenance: page identity/link,
displayed version, last-modified time, extraction time and total available versions where obtainable.
Do not assume a version number equals the number of retained versions.

Provide fuller metadata for one page or a batch. Historical reads and comparisons are later work;
batch exports should accept selected pages and versions without silently substituting another version.

Provide direct agent-readable results and a separate batch extraction operation. The latter accepts
pages/versions and a parsing/fetching scope, and produces one root ZIP with text files and optional
attachment folders, following the established Teams/Outlook export conventions. Byte-by-byte
attachment deduplication is not required.

**Decided 2026-10-19 (D8), export layout:**

- One page with no attachments selected is written as a single file; anything else as one `.zip`.
- Each page is one file at the ZIP root named `<Title> (<page id>) v<version>`, with characters
  Windows does not allow replaced and long titles shortened. Pages stay flat, with no tree folders.
  Each file's header carries the ancestor breadcrumb (`Space › Parent › Page`).
- Attachments, when the scope includes them, go in a folder with the page file's name minus its
  extension.
- `text` exports are `.txt` files with exactly the `get_pages` text output, without a character limit.
  `rich` exports are `.html` files with the storage body unchanged, a metadata comment at the top with
  the same header facts, and the comments in a final section.
- `_export-report.txt` at the root, present only when something failed or was skipped, lists each
  page or attachment and the reason.
- Output goes to the per-user output folder used by the other connectors, one timestamped file per
  export; the tool returns the path.

**Decided 2026-10-19 (D4), limits and parse scope:**

- `get_pages` takes up to 20 page IDs or URLs and a `max_chars` budget (default 40,000, maximum
  400,000), returning pages in the order given. Whole pages are packed into the budget; the first is
  always returned. Pages that did not fit are listed in `remaining`, never dropped.
- A page larger than the budget is cut at a block boundary (paragraph, list item, table row). Its
  cursor carries page ID, version and offset; continuation reads that same version, through a
  historical read if the page changed. Nothing is kept in process memory between calls.
- The parse scope is an object. Defaults: `comments` (footer and inline, resolved included),
  `attachments` (list only), `macros_from_storage` and `header`, all on.
- Pages in one call are fetched with bounded concurrency (6, per the transport defaults).
- `export_pages` always writes every selected page in full, with no character budget. It accepts at
  most 500 pages per call and rejects larger requests before starting. Pages that fail are listed in
  the export with the reason. Its tool description warns that large exports take minutes.

Attachments can be explicitly retrieved or included by export scope. File parsing, transcription,
OCR and multimedia interpretation are out of scope for now. Ordinary direct reads do not
automatically download attachments.

## 6. Page and draft management

Daily-use readiness requires search, full reads, tree access, page moves, and create/read/update/delete
operations for drafts and published pages. This is broader than the current read-only implementation.

D5–D9 below settle body formats, edits, undocumented routes, placement, moves and deletion. Drafts
are new-page drafts only. Draft listing and deletion and trash restore are still open
([roadmap](roadmap.md)). Purge stays excluded. Test writes only against disposable synthetic pages.

**Decided 2026-10-19 (D9), deleting a page with children:** `delete_page` moves pages to the trash
with the documented `DELETE`, never purges, and takes `children`:

- `refuse` (default): fails when the page has children, listing them. Confluence's own behavior
  leaves children without a parent in the page tree, which is never applied silently.
- `move_up`: re-parents the children under the deleted page's parent (or the space root), keeping
  their order, then trashes the page.
- `trash`: trashes the whole subtree bottom-up. Subtrees over 100 pages are refused, with the count.

The result lists the page and every descendant affected. A failed step stops the operation and
reports which pages were moved or trashed and which were not; nothing is undone automatically, and an
uncertain step is reported, not retried.

**Decided 2026-10-19, delete and restore:** Confluence always restores a page to the space root,
whether it was deleted through REST or the UI, and restoring needs space-admin rights.

- `delete_page` first checks the user's `delete` operation on every affected page and refuses before
  any write if one is missing. Its result reports each page's former parent and position. When the
  space permissions cannot be read (the user is likely not a space admin), it warns that restoring
  will probably not be possible, so the agent can treat the delete as one-way.
- Best effort: before trashing, `delete_page` stores each page's placement in a content property on
  the page. Failing to store it is logged and does not stop the delete.
- `restore_page` uses the documented restore, then places the page: an explicit `placement` from the
  caller first, otherwise the stored property, otherwise the space root. Any failure reading or
  applying the property (missing, unreadable, parent gone or trashed, move refused) is logged and the
  page stays at the space root. The property is removed after a successful restore where possible. The
  result states where each page ended up and why.
- `list_trash` lists trashed pages per space or across all visible spaces (title, space, last edit,
  page ID). Confluence does not expose when or by whom a page was deleted.

**Decided 2026-10-19 (D5), body formats**, mirroring the Outlook connector's text-or-HTML bodies:

- `create_page` and `update_page` take `body` and `format`. `rich` (default) is HTML plus Confluence
  storage tags, written as Confluence storage. `text` is escaped and split into paragraphs on blank
  lines. No wiki markup or Markdown input.
- Before sending `rich`, the connector applies a small set of documented conversions, because AI tools
  produce HTML that Confluence stores but renders poorly or rejects:
  - `<pre><code class="language-X">` and plain `<pre>` become the code macro, with the language when
    given.
  - HTML5 void elements (`<br>`, `<hr>`, `<img>`) are self-closed.
  - `<details>`/`<summary>` become the expand macro.
- `rich` input is rejected before sending if it contains active content: `<script>`, `<style>`,
  `<iframe>`, `<object>`, `<embed>`, `on*` attributes, `javascript:` URLs or the HTML macro. Confluence
  passed these through in testing. Any other markup Confluence cannot parse returns its 400 message
  with row and column; the connector does not repair it further.
- The write tools' descriptions carry a short storage reference (code macro, panels, expand, status,
  table of contents, task list, page and attachment links, images, mentions) so agents can use the
  full range of Confluence formatting.
- `get_pages` takes `format`: `text` (default, the D3 output) or `rich`, which returns the storage
  body unchanged for editing, like Outlook's `body="html"`.

**Decided 2026-10-19 (D6), edits:** `update_page` always replaces the whole body (and optionally the
title), as edits do in the Teams and Outlook connectors. There are no section, append or
find-and-replace modes. Confluence-specific safeguards:

- The call requires `expected_version`. A page changed in the meantime fails with Confluence's 409
  and is never overwritten or retried.
- Before sending, the connector compares the stored and new bodies and reports any inline-comment
  anchors, macros and attachment references that would disappear. Dropping inline-comment anchors
  detaches those comments, so such a write is refused unless the call passes
  `allow_comment_loss: true`.

**Decided 2026-10-19 (D7), undocumented write routes:**

- Avoid any route, field or feature known to be deprecated or outdated, documented or not. Prefer
  documented routes; use an undocumented one only for a required capability with no documented path,
  isolated in one place, with its failure reported and no silent fallback. Routes marked experimental
  (such as the version list) are flagged in the research record.
- No unpublished edits of published pages: the editor-draft route cannot be read back and belongs to
  the non-collaborative editor. Agents propose changes as new-page drafts or edit with
  `expected_version`.
- Pages are identified by page ID, which is unique across the instance and survives renames and
  moves. Titles are unique only within a space and change on rename. Tools accept page IDs or any page
  URL form and resolve them to IDs first. `/spaces/KEY/pages/ID/…` and `viewpage.action?pageId=` give
  the ID directly; the title in the path is ignored. A `pageVersion` in the link selects that
  version. `/x/…` short links are followed. `/display/KEY/Title` needs a lookup by space and title and
  fails clearly after a rename. Anchors and comment focus are ignored. Links to any host other than
  the configured base URL are refused.
- `create_page` and `move_page` take one placement: `parent_id` (or a parent URL), from which the space
  is derived, or `space_key` alone for the space root, plus an optional position (`first`, `last`,
  or `before`/`after` a sibling of the same parent). A `space_key` that disagrees with the parent's
  space fails the call. A title that already exists in the target space fails before any write, with
  the existing page's link. The connector makes every call needed to reach the placement. Same-space parent changes use the documented content update
  (`ancestors`); positioning uses the UI's `movepage.action`. If positioning fails after the page was
  created or moved, the page stays and the result says the position was not applied.
- Cross-space moves use `movepage.action` with the target `spaceKey` and poll the documented
  `/rest/api/longtask/{id}` until it finishes. Verified 2026-10-19: page ID, version, children,
  attachments, comments and labels are kept. If Confluence refuses a cross-space move (for example
  for lack of permission), the tool description tells agents to recreate the page in the target space
  (body, labels and attachments) and then trash the original, accepting that comments, history and
  the page ID are lost.

## 7. Authentication and concurrent use

Use a user-generated Confluence token without requiring administrator registration or assistance.
Existing PAT probe success is the starting evidence; exact lifecycle and policy must be verified.
Confluence authentication is separate from Microsoft OAuth; shared Microsoft sign-in is not assumed.

Provide persistent encrypted authentication and explicit logout, with no plaintext fallback. Before
logout, explain local token deletion and any need to generate a replacement when the original cannot
be recovered. Distinguish local removal from server revocation; revocation behavior remains open.

Coordinate shared authentication state and output files across processes. Preserve account boundaries
and ensure running processes respect logout/replacement before their next acquisition.

**Decided 2026-10-19 (D1):** store the token with `msal-extensions` encrypted persistence and its
cross-process lock, as `lrh-m365-auth` does; no Microsoft sign-in is involved. If encryption is
unavailable, refuse to run. Read the stored token on every Confluence call rather than keeping it in
process memory. The command line matches the Microsoft 365 connectors: `confluence-connector auth`
prompts for the token without echoing it, verifies it with the current-user endpoint and stores it
with that username. `confluence-connector auth --sign-out` warns that the token cannot be recovered,
then deletes it locally; it does not revoke it in Confluence. `status [--check]` reports the stored
account offline, or verifies it, and `mcp` runs the stdio server. Tokens never pass through MCP tools.

## 8. Reliability and acceptance

Report inaccessible pages, unsupported elements, failed downloads and partial coverage explicitly.
Retry reads a bounded number of times. Retry writes only when known not to have applied, honoring
`Retry-After` for 429/503. Never resend an uncertain write; report it for inspection. Keep error paths
short and readable, with no private content in operational logs.

**Decided 2026-10-19, transport defaults** (from the
[concurrency measurements](api-research.md#search); the PAT run reached 10 in parallel without
throttling; 6 per tool call matches one browser tab on HTTP/1.1):

- As in the Microsoft 365 connectors, one lazily built async `httpx` client per MCP process, kept
  across tool calls (keep-alive) and rebuilt when the stored account changes. HTTP/1.1 only. At most
  6 connections. One limit of 6 requests in flight per tool call, shared by every page
  and every part of each page. Each chat runs its own connector process and limits are not
  coordinated across processes; a few concurrent chats stay within the tested 10-parallel range, and
  429 handling covers the rest.
- Each page is read as up to three parallel requests: bodies with version and history; attachments
  (`child/attachment?limit=500`); comments (`child/comment?depth=all`). Parts switched off by the parse
  scope are skipped. Embedded `children.*` expansions are not used: they stop at 25 and omit replies.
- Timeouts: 10 s to connect, 30 s to read, 10 s waiting for a pooled connection.
- Reads retry up to 3 attempts with short jittered backoff on connection errors, timeouts, 429, 500,
  502, 503 and 504, honouring `Retry-After`. A 500 that persists across the attempts is reported with
  Confluence's log referral. Writes follow the shared write-retry rule: retried only when the request
  provably never applied (not sent, or answered 429 or 503). A timeout or any other uncertain outcome is
  reported for inspection, never resent.

Document potentially slow operations so agents can choose them knowingly; do not add a permission
prompt solely because a query is broad. Cancellation behavior still needs verification, especially
when a remote write may already have completed.

Use synthetic offline fixtures and iterative, explicitly authorized UI/API checks. Captures and UI
resource exports are temporary private investigation inputs outside the repository. Release evidence
must cover representative searches/reads, trees, draft/page operations and moves, authentication,
concurrent use and exports.

**Decided 2026-10-19, acceptance:** test functionality against synthetic fixtures with known facts,
never against real wiki content or its ranking.

- A setup script under `research/`, run only on request, creates or refreshes a `Connector Lab
  Acceptance` tree in the personal space. It covers unique markers, a punctuated title, a code
  identifier, accented words, every macro type, merged and nested tables, an inline image and an
  attachment, a footer comment with a reply, a label, ordered children with a grandchild and several
  versions. An inline comment is added by hand, because only the UI creates one. A second approved
  space for cross-space moves is a private setting. Missing manual steps or settings make the
  dependent checks report `skipped`.
- Checks cover search modes and scope filters (tolerating indexing delay), cursor paging, link and
  version resolution, reading output per D3/D4, tree order, metadata, export layout per D8, and every
  write path including conflicts, refusals, the loss check, cross-space moves and the three
  `delete_page` modes.
- The same checks run offline against synthetic fake responses before every commit, and live only
  when explicitly authorized, reporting pass, fail or skipped with timings and no page content.
- Ready for daily use means the offline suite passes and every live check passes or is skipped for a
  stated reason.

Version comparisons, activity feeds, file interpretation and automated documentation-quality analysis
are follow-ups. The agent, rather than the connector, decides how to reconcile conflicting sources.
