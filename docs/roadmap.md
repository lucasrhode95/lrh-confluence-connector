# Confluence connector roadmap

Open work only. Settled requirements are in [requirements](requirements.md); the current implementation
is in [architecture](architecture.md); dated evidence is in [API research](api-research.md).
Delete items when complete. Snapshot **2026-10-20**.

Work is split by what it needs: **implementation** is code still to write; **decisions** wait for the
user; **live verification** checks something on the real instance before it is built or relied on.

## Implementation

### Implemented tools with live acceptance pending

Names follow the sibling connectors (no product prefix; the MCP server name namespaces them). The
proof-of-concept tools have been removed. These tools are implemented locally with offline
coverage; live acceptance remains open. The acceptance runner requires `--allow-writes` for
Confluence writes.

| Tool | Purpose | Status |
|---|---|---|
| `create_page` | New page or new-page draft under a parent, in `rich` or `text` format | Implemented locally; live acceptance pending |
| `update_page` | Replace title and/or whole body with an explicit `expected_version` | Implemented locally; live acceptance pending |
| `publish_draft` | Publish a new-page draft, keeping its ID | Implemented locally; live acceptance pending |
| `list_drafts` | The user's new-page drafts | Implemented locally; live acceptance pending |
| `delete_draft` | Permanently delete a new-page draft (bypasses the trash) | Implemented locally; live acceptance pending |
| `set_labels` | Add or remove page labels | Implemented locally; live acceptance pending |
| `move_page` | Change parent, space or sibling order | Implemented locally; live acceptance pending |
| `delete_page` | Trash a page and handle its children | Implemented locally; live acceptance pending |
| `list_trash` | List trashed pages | Implemented locally; live acceptance pending |
| `restore_page` | Restore a page with placement fallbacks | Implemented locally; live acceptance pending |

Comment and attachment writes are not in the requirements and are not proposed here.

### Foundations shared by all tools

- Live acceptance for every page, draft and structure write remains pending. Shared retry and
  uncertain-write behavior is implemented and has offline tests.

### Review findings (2026-10-20)

From whole-project reviews of `e67b60d` by Codex and Claude, plus the unresolved review threads on
PRs #5–#9 that are still open in the code.
Group related items into design fixes rather than one patch each.

**Bugs**, highest first:

- [P1] Paging can stop before delete preflight reads the whole subtree: child-page mapping drops
  `_links.next` and traversal treats a page under 500 as final, so `children=trash` can report success
  while leaving descendants behind. Space listing has the same assumption. Keep the continuation
  signal and advance on actual `start + size` (`service/structure.py`, `remote/confluence.py`).
- [P1] Every live acceptance run fails: the authentication and fixture checks pass the coroutine
  `_pass()` where `AcceptanceReport.run` expects a callable, so both record a `TypeError` and the run
  exits 1. Pass `_pass` (`research/run_acceptance.py`, from PR #8).
- [P2] `delete_page` with `children: "move_up"` can undo a concurrent reparent: each child's fresh
  metadata is checked only for being current, then moved under the deleted page's former parent.
  Skip a child whose parent or space no longer matches the captured node (`service/structure.py`,
  from PR #7).
- [P2] `restore_page` cannot take a `/display/SPACE/Title` URL: the resolver looks the title up
  through CQL, which excludes trashed content, so it always fails with `NotFound` despite every URL
  form being promised. Resolve it against the trash listing or require an ID-bearing URL and document
  that (`service/structure.py`, `remote/links.py`, from PR #7).
- [P2] Search: a term with inner punctuation (`ci-cd`, `release/notes`) is matched as either half,
  not as one term. Live 2026-10-20: `ci-cd` matched 2,160 pages, `"ci-cd"` 482. Quote such terms in
  `match: "all"` and `"any"` (`service/search.py`).
- [P2] External attachment links can take a local attachment's identity: attachment classification
  and filename matching run before origin validation, and included-page attachments are ambiguous
  the same way. Validate origin and owner first (`service/page_text.py`).
- [P2] `set_labels` can silently skip a removal when labels are paginated: metadata reads only the
  first expanded labels page, so an unseen label counts as absent. Read the full label set or stop
  claiming absence (`remote/confluence.py`).
- [P2] Moving to the space root depends on the unverified `ancestors: []`; with position `first` and
  no siblings it is the only write, so the result can claim a move that did not happen. Use
  `movepage.action` with `topLevel`, or verify live first; the test asserting the empty list then
  changes (`remote/confluence.py`, `service/structure.py`).
- [P2] Timezone calibration assumes the sample page is not edited during the search; one concurrent
  edit made a UTC instance measure `UTC+00:01`, which would then be written to the config. Re-read the
  sample's timestamp and retry or abandon on change (`service/timezone_calibration.py`).
- [P2] A malformed credential record also blocks `auth --sign-out`, because sign-out decodes the
  record before clearing it. Let sign-out clear it, keeping encryption and locking (`auth/store.py`).
- [P2] A malformed URL (`https://[broken`) raises `ValueError` from `urlsplit`, so one bad reference
  aborts a whole batch. Convert it to `InvalidRequest` (`remote/links.py`).
- [P2] Acceptance setup cannot create its root fixture: the fixture body includes the HTML macro,
  which the write service refuses. Make the fixture match the write contract and handle refused
  cases explicitly (`research/acceptance_fixtures.py`).
- [P2] The live move and order check passes even when positioning fails: it reads the returned parent
  but never the sibling order, and cross-space checks trust the service's reported target. Re-read
  metadata and ordered children independently before cleanup (`research/run_acceptance.py`).
- [P2] A successful `--allow-writes` acceptance run leaves restored scratch pages current: the
  `move_up` parent and the three subtree pages are restored but never re-trashed, so they accumulate
  in the personal space and a reused run ID hits duplicate titles. Re-trash them after their
  assertions (`research/run_acceptance.py`, from PR #8).
- [P2] Page text, from PR #6: rendered Jira content becomes `[Unrendered macro: jira]` when there is
  no storage key (storage disabled, or a macro in an included page); images with only
  `data-linked-resource-filename` lose their filename; links whose ID is only in `data-page-id` lose
  their page reference; a code line starting with ``` or `~~~` can split one code block into two
  budget blocks (`service/page_text.py`).
- [P3] `delete_page` reports definite failures as `not_attempted`; use `failed`, keeping `unknown`
  for uncertain outcomes (`service/structure.py`).
- [P3] The missing-`cql_timezone` error says to run `status --check`, but running MCP processes cache
  the config; also say to restart the MCP client (`service/cql_dates.py`).
- [P3, POSIX] A single-file export replaces the owner-only claimed file with a staging file written
  under the normal umask, so it can end up `0644` (`service/exports.py`).

**Simplification and efficiency:**

- Same-space moves make two writes by default: position defaults to `last`, so `movepage.action` runs
  after every documented parent change, adding a page version and a failure point. Use one route per
  move (decide with the user, since D7 named the documented route for parent changes).
- Unify page-reference validation, space-key validation, the current-page check, the account-error
  passthrough, filename sanitization (attachments and exports), `_cql_literal` and same-instance URL
  parsing (`remote/links.py`, `remote/confluence.py`, `service/page_text.py`). These are repeated
  across services and already differ in control-character handling and errors.
- Share placement resolution between create, move and restore (`_Placement`/`_create_placement` and
  `PagePlacement`/`_resolve_placement`).
- Replace the hand-written `HTMLParser` in `service/write_markup.py` (362 lines) with BeautifulSoup,
  already a dependency, as was done for page text.
- Parse storage once per page instead of separately for macros and references, and skip Markdown
  conversion for `rich` reads, whose output is the storage body.
- Merge the duplicated retry loop of `ConfluenceTransport.download_to` into `_exchange`.
- Remove dead paths: the unused helper in `service/writes.py`, the unreachable ownership branch in
  `bootstrap.py`, and the unneeded `async` on `_nodes_for_children`.
- Add a lightweight undefined-name check (for example `pyflakes`) to the documented checks:
  `service/pages.py` raises `Upstream` without importing it, which compilation does not catch.

**Tests:**

- Replace the acceptance-fixture tests that only check macro names with a fake-transport run of the
  setup's real write path (it would have caught the fixture bug above).
- Turn similar cases into tables: uncertain-write outcomes (timeout, oversized 2xx, oversized 5xx,
  5xx) and account changes after a write (`test_remote.py`); label partial failures (ordinary,
  account change, uncertain) and draft cursors (`test_writes_service.py`); config precedence
  (`test_config.py`); MCP tool round-trips in `test_app.py`, moving the export attachment-reuse case
  to the exports tests.
- Remove the trash assertions duplicated in the omnibus route test (`test_write_remote.py` keeps its
  focused trash test) and assertions that only inspect embedded constants, such as the fixture PNG's
  signature.
- Keep the golden renderer output, typed-reference checks, account-boundary checks and
  uncertain-write checks.

**Documentation:**

- README: the architecture link points to a missing `#6-mcp-tools` anchor.
- Requirements: historical reads are called later work although versioned body reads exist; the
  transport decision says six requests per tool call and a client rebuilt on account change, while
  the code shares one process-wide limit of six and keeps the transport.
- Roadmap: token-lifecycle checks still wait on D1, which is built, and completed implementation is
  repeated as open work. Validations mixes settled implementation choices with real live checks;
  keep only what needs live verification and move contract details to the architecture.
- `research/README.md` calls the configuration loader stdlib-only, but it now imports
  `msal_extensions`.
- Keep `requirements.md` (434 lines) direct and summarized: fold the dated restatements into the
  current rules.

## Decisions

None open. Settled decisions are in [requirements](requirements.md).

## Live verification

Each check needs authorization for the exact requests. Synthetic `Connector Lab` fixture pages remain
in the personal space, including a rich-text sample page.

### Validations

- `list_spaces` requests `current` and `archived` separately. The Data Center REST reference documents
  the `status` parameter but does not enumerate its accepted values; this filter choice is inferred
  from the returned status names and the planned tool contract. Verify both result sets on the
  configured instance during user-run acceptance.
- `search_content` fixes the scope input names as `space_keys`, `space_type`, `content_types`,
  `labels`, `creator_usernames`, `contributor_usernames`, `created_after/before`,
  `modified_after/before`, `parent`, `ancestor` and `include_archived`. Confirm calibration and
  `cql_timezone` conversion on the configured instance, and whether CQL uses the profile or server
  zone if they ever differ. Confirm conservative widening during repeated daylight-saving hours.
  The tool accepts up to 100 results per page, 50 values per collection filter and 255 characters per
  filter value; query text is capped at 1,000 characters. Cursors are limited to 16,384 characters
  and offsets to 1,000,000. These bounds protect request and response sizes.
- Quick-navigation title rows are mapped from the observed `contentNameMatches` envelope using
  `contentId/id`, `title/name`, `spaceKey` or nested `space`, and `url/href`. This route and row shape
  are browser-observed and deployment-specific; verify result coverage and returned fields on the
  configured instance. The supplement is skipped for scoped title searches because quick navigation
  does not accept those filters. Merged first-page hits stay within the caller's result limit, preserving
  CQL order; only returned quick-navigation IDs affect continuation filtering. Search terms escape
  text-query syntax separately from CQL string quoting; verify metacharacter behavior on the configured
  instance. User lookup continues to use the CQL search and exact `/rest/api/user?username=` lookup for former
  users; quick navigation with `type=user` is known to fail.
- `get_page_tree` uses `rest/api/space/{spaceKey}/content/page?depth=root` for space-root pages and
  `rest/api/content/{id}/child/page` for descendants. Verify space-root response order and paging on
  the configured instance. Page depth is 1–5 (default 3), counting the selected page as level 1;
  for a space root, top-level pages are level 1. A result is capped at 1,000 pages; the cap is
  intended to prevent oversized MCP responses. Space keys accept letters, digits, underscores and
  hyphens, up to 255 characters, with one optional leading `~` for a personal space. Confirm the
  configured personal-space key and its `~` form on the target instance; verify the fixture runner's
  personal and cross-space settings.
- Structure writes have offline coverage only. During user-run acceptance, verify same-space parent
  changes, first/last and before/after ordering, cross-space long-task completion/failure, delete
  operation preflight, trash permission warnings, property persistence across trash, and restore
  placement fallbacks. The implementation reads up to 5,000 pages in a hierarchy or direct child
  list before it stops without writing; confirm that read cap is practical on the configured
  instance. Cross-space move status is polled up to 40 times at 250 ms intervals and a 10-second
  wall-clock bound; confirm the instance's terminal long-task fields and that the wait is practical.
  Trash listing uses 100 rows per request, a
  1,000,000 offset cap and stateless cursors; confirm paging totals and visible-space coverage.
- `get_page_metadata` accepts up to 20 references and returns successful records alongside per-page
  failures. Each record lists up to 5,000 experimental versions (200 per request) and reports when
  that cap truncates history. Metadata reports the current page and rejects versioned references;
  historical reads remain separate later work. Confirm the expansion fields, contributor mapping
  and version-list totals for the configured instance during acceptance.
- `get_pages` reads `body.view`, optionally `body.storage`, `child/attachment?limit=500` and
  `child/comment?depth=all&expand=body.view,history`. Verify actual attachment and comment counts, the
  comment ancestry/inline-selection fields, and the page history field (if any) that reports total
  versions. Confirm historical reads continue the selected page version after a later edit. Confirm
  whether an authoritative source-position mapping is available for HTML macros when the view has no
  macro wrapper. Until then, unmatched HTML macros are marked after the body; matching visible text or
  script position is unsafe because neither establishes identity. Confirm stable identity mapping for
  macros inside resolved includes; those wrappers do not consume the requested page's storage metadata.
  Attachment and comment lists are fetched again by a continuation, so their content may change while
  the page body remains version-pinned. Check whether either child route can exceed its 500-item
  request limit on the configured instance.
- `download_attachments` accepts a page and selected attachment IDs; verify that the 500-ID request
  cap and per-file failure behavior fit expected use. `export_pages` excludes attachment bytes by
  default and includes them only when `include_attachments` is true. Confirm the single-file/ZIP
  boundary (including an empty attachment selection), forced provenance header, duplicate-name suffix,
  attachment-list cap report and rich-comment section are clear to agents. Verify the 240-byte/UTF-16
  filename bound on supported filesystems.
- `find_users` requires either a display-name `query` or exact `username`; the modes cannot be mixed.
  Name queries and usernames are capped at 255 characters, results at 100 per page and offsets at
  1,000,000. Verify current and former-user results during acceptance.

- Task 5 writes have offline synthetic coverage. Before release, verify their REST routes and D5
  conversions on the configured instance during user-run acceptance, including stale-version 409
  behavior, inline-comment refusal, permanent draft deletion and partial label reporting. Confirm that
  first/before/after placement works for current pages and drafts, whether duplicate-title preflight
  should include hidden draft titles, and whether the 2,000,000-character body cap and draft-page
  bounds fit expected use. Verify application-level move failures appear as placement warnings, and
  account changes during multi-step writes preserve confirmed partial results without further writes.
  Verify partial label results on the configured instance after definitive and uncertain failures;
  successful operations remain visible and are not automatically reversed.

### Other live checks

- Token lifecycle (expiry, revocation visibility) and concurrent processes once D1 is built.

Resource bundles expose UI code, not successful requests. Record dated results and limitations in the
research record.
