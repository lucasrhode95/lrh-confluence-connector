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

### Attachment upload

Pending the decision below. An `upload_attachments` tool takes one page reference and local file paths.

- **Create or version:** a new filename creates an attachment; an existing one adds a version through
  `child/attachment/{id}/data`. Optional version comment.
- **Transport:** reuse the multipart path from `research/setup_acceptance.py` (`X-Atlassian-Token:
  no-check`, account check, write retry policy). Never resend an upload whose outcome is uncertain.
- **Preflight:** files exist and are readable, size within a configured bound, media type from the
  extension, no duplicate filenames in one call.
- **Result:** per file, attachment ID, version and download URL, or the failure. No rollback of files
  already uploaded.
- **Docs and tests:** synthetic-byte tests for create, new version, refusal and uncertain outcome;
  update requirements, architecture and README.

## Decisions

- **Attachment upload:** requirements §4 lists attachment writes as a non-goal. Pages that embed
  diagrams need it: on 2026-10-26 a PNG and its SVG source were uploaded with a one-off script reusing
  `ConfluenceTransport`. Decide whether to allow uploads only; comment writes stay out of scope.

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
- **Attachment upload:** once implemented, check the instance's size limit, new-version behavior on an
  existing filename, and SVG/PNG media types in a disposable fixture page.
- **Client setup:** after registration and a full restart, confirm tool discovery and a small approved
  read in Claude and Codex. Local protocol discovery alone does not verify instance access.
