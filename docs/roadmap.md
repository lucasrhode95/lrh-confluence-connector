# Confluence Connector Roadmap

Open implementation, decisions and live verification. Requirements own behavior; architecture owns
design; research holds dated integration evidence. Merged work is available in Git/PR history.
The review fixes below remain until the user reviews this pass. Snapshot **2026-10-21**.
Offline checks: 290 tests pass (one platform skip), compilation, pyflakes and probe CLI help.

## Implementation

### Review fixes awaiting user review

| Finding | What changed and how it is checked offline |
|---|---|
| Compressed JSON decoded twice | Snapshot decoded bytes without compression headers; gzip read/write regressions verify one request each. |
| Async move returned before completion | Require terminal task completion; a successful-but-18%-complete response now keeps polling. |
| Exact title lookup missed quotes and archived content | Use the content collection for current/trashed titles; tests verify quoted titles and status-specific routes. Archived-space coverage remains a live check. |
| Short/empty collection pages hid incomplete results | Share validated envelopes and continuation offsets; attachments/comments page to 5,000 items or fail explicitly. Tests cover short pages and empty continuations. |
| Content-loss checks ignored target ownership | Include owning page and space in attachment/macro identities; regressions change only the owner/space. |
| Complex tables lost external links | Preserve passive link destinations and whole table budget blocks. Ordinary tables use markdownify; merged/nested/code tables retain minimal HTML. Golden and focused tests cover output. |
| Acceptance failures left scratch pages | Track confirmed creations, return foreign-space pages before trashing, and clean on definite failure. Stop for inspection on uncertain writes/account changes; synthetic tests cover both. |
| PAT could follow a changed instance config | Bind encrypted records to canonical instance URL/context and account. Mismatches and unbound records fail closed; sign-out works without config. Store tests cover switching and URL normalization. |
| Move acceptance omitted version invariance | Independently compare version before/after same-space moves, alongside parent/order. Synthetic version drift is rejected. |

### Simplifications awaiting user review

- Shared public reference/placement validation, collection envelopes and draft/trash request code.
  Draft and trash MCP tools retain distinct names; the common route is internal.
- Removed unreachable restore compensation and its unused partial status, duplicate reference
  deduplication and test-only storage wrapper, repeated Pydantic defaults and annotation constants.
- FastMCP serializes typed results and publishes output schemas; tests check schemas and actual tool
  round trips. General server instructions no longer repeat every tool description.
- Multipart fixture uploads use the product retry/account policy. Download batches scan the output
  folder once. Export collisions use source metadata and shared filename bounds.
- Removed redundant fixture-string/upload-retry checks and replaced a hard-coded export year assertion.
  Requirements, architecture, research and README were consolidated; the completed UI session itinerary
  and duplicated per-tool manual were removed.

### Attachment management

Pending the decision below. Reads exist (`get_pages` lists attachments; `download_attachments` fetches
bytes). Writes are missing: on 2026-10-26 a doc refactor needed upload, new versions and removal, and
used one-off scripts over `ConfluenceTransport` for all three.

- **Create and version:** `upload_attachments` takes one page reference and local file paths. A new
  filename creates an attachment; an existing one adds a version through `child/attachment/{id}/data`.
  Optional version comment.
- **Update metadata:** rename an attachment and change its comment or media type without new bytes.
- **Trash and restore:** `trash_attachments` moves attachments to the trash and `restore_attachments`
  brings them back. No permanent purge, matching the page rule.
- **Safety:** IDs must belong to the named page. Before trashing, refuse attachments the current body
  still references unless explicitly allowed, and report pages elsewhere that link to them when known.
- **Permissions:** surface the attachment-delete permission separately. On 2026-10-26 trashing worked
  on one page and was refused (403) on another where the same account had just uploaded files.
- **Transport:** reuse the multipart path from `research/setup_acceptance.py` (`X-Atlassian-Token:
  no-check`, account check, write retry policy). Never resend a write whose outcome is uncertain.
- **Preflight:** files exist and are readable, size within a configured bound, media type from the
  extension, no duplicate filenames in one call.
- **Result:** per attachment, ID, version and download URL, or the failure. No rollback of
  attachments already changed.
- **Docs and tests:** synthetic tests for create, new version, rename, trash, restore, refusals and
  uncertain outcomes; update requirements, architecture and README.

### Comment management

Approved by the user on 2026-10-28. Reads exist (`get_pages` returns threaded footer and inline
comments with IDs). Writes are missing: on 2026-10-28 editing the user's own footer comment needed a
browser call to `PUT /rest/api/content/{id}` (type `comment`, container page, version + 1,
`X-Atlassian-Token: no-check`), which worked.

- **Create:** `add_comment` takes one page reference, a body in `rich` or `text` and an optional parent
  comment ID for a reply. Promote the fixture-only `create_page_comment` to this product path, with
  the same body validation as page writes. Inline comments stay UI-only (they need a text anchor).
- **Read one:** `get_comment` returns one comment with its version, page, parent, author and
  resolution, for edits by ID without reading the whole page.
- **Update:** `update_comment` takes the comment ID, the new body and `expected_version`. Stale
  versions are refused, as for pages. Works for footer and inline comments; inline anchors stay.
- **Resolve and reopen:** inline comments only, if the instance exposes resolution through REST.
- **Delete:** `delete_comment`. Confluence may delete comments permanently rather than trash them;
  confirm first (see Live verification). If permanent, require an explicit confirmation flag, and
  refuse comments with replies unless allowed.
- **Safety:** account check on every write; when a page reference is given, the comment must belong to
  it. Surface 403 as a permission refusal. Never resend a write whose outcome is uncertain.
- **Result:** comment ID, version, page and URL, or the failure.
- **Docs and tests:** synthetic tests for create, reply, update, stale version, resolve, delete,
  refusals and uncertain outcomes; change requirements §4 so only attachment writes stay a non-goal
  (unless that decision lifts too); update architecture and README.

## Decisions

- **Attachment management:** requirements §4 lists attachment writes as a non-goal. Documentation
  work needs create, version, update, trash and restore (see Implementation). Decide whether to lift
  the non-goal for attachments. Comment writes are approved (see Implementation).

A whole-page cleaned-HTML read format is not implemented; `rich` remains verbatim Confluence storage
for editing. Teams HTML conversion is tracked in that connector's roadmap.

## Live verification

No live Confluence requests were made in this implementation pass. Each check needs explicit
authorization for reads/writes and targets; synthetic pages moved to another space must be returned
and removed immediately. Use the [research toolkit](../research/README.md#synthetic-acceptance-fixture).

- **Updated acceptance:** verify these fixes on the configured instance, including compressed replies,
  terminal move state, paged attachments/comments, no-version same-space moves and failure cleanup.
  Earlier acceptance and targeted date/cross-space checks passed on 2026-10-20; inline-comment marker
  and loss-refusal checks still need an operator-added inline comment in the disposable fixture.
- **Archived spaces and titles:** compare current/archived lists with the UI, direct exact-title
  resolution in archived spaces, and quick-navigation coverage for personal/archived spaces and quotes.
- **CQL timezone:** confirm calibration/conversion on another zone and whether CQL follows profile or
  server zone when they differ; check repeated daylight-saving hours.
- **Search behavior:** check `siteSearch` ordering and `content.version.by` across supported Data Center
  versions; the read-only check on the configured instance passed (research §4.6).
- **Large child lists:** verify paging beyond 500 children and practical tree/delete preflight caps.
- **HTML macros/includes:** investigate whether source fields locate wrapperless HTML macros and
  identify macros inside resolved includes.
- **Refused cross-space moves:** inspect terminal failure fields and whether the 10-second bound fits
  large subtrees. A pending outcome must never trigger compensating writes.
- **Token lifecycle:** verify expired/revoked PAT responses and cross-process sign-out with the bound
  credential format.
- **Write bounds and parser recovery:** check real body/title/label limits, whether duplicate-title
  preflight should include hidden drafts, and whether BeautifulSoup repair of malformed rich markup
  is acceptable compared with Confluence's native parse errors.
- **Attachment management:** once implemented, check in a disposable fixture page the instance's size
  limit, new-version behavior on an existing filename, SVG/PNG media types, rename, trash and restore,
  and what decides the attachment-delete permission refused on 2026-10-26.
- **Comment management:** once implemented, check in a disposable fixture page whether deleting a
  comment trashes or purges it, whether inline comments can be updated and resolved through REST,
  what happens to replies when a parent is deleted, and editing another user's comment (expect 403).
- **Client setup:** after registration and a full restart, confirm tool discovery and a small approved
  read in Claude and Codex. Local protocol discovery alone does not verify instance access.
