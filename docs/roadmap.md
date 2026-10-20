# Confluence connector roadmap

Open work only. Settled requirements are in [requirements](requirements.md); the current implementation
is in [architecture](architecture.md); dated evidence is in [API research](api-research.md).
Delete items when complete. Snapshot **2026-10-19**.

Work is split by what it needs: **implementation** is code still to write; **decisions** wait for the
user; **live verification** checks something on the real instance before it is built or relied on.

## Implementation

### MCP tools pending local implementation or live verification

Names follow the sibling connectors (no product prefix; the MCP server name namespaces them). The
proof-of-concept tools have been removed. **Ready** means evidence is enough to build the tool;
**Implemented locally** means offline coverage exists but live acceptance remains pending. Every
write tool also waits on V2, because writes were verified only through a browser session.

| Tool | Purpose | Status |
|---|---|---|
| `create_page` | New page or new-page draft under a parent, in `rich` or `text` format | Implemented locally; live acceptance pending |
| `update_page` | Replace title and/or whole body with an explicit `expected_version` | Implemented locally; live acceptance pending |
| `publish_draft` | Publish a new-page draft, keeping its ID | Implemented locally; live acceptance pending |
| `list_drafts` | The user's new-page drafts | Implemented locally; live acceptance pending |
| `delete_draft` | Permanently delete a new-page draft (bypasses the trash) | Implemented locally; live acceptance pending |
| `move_page` | Move to a target space, parent (or space root) and optional position, as one call | Ready |
| `delete_page` | Trash a page after a per-page `delete` check, with `children`: refuse (default), `move_up` or `trash`; reports former placement and a one-way warning | Ready |
| `list_trash` | Trashed pages per space or across visible spaces | Ready |
| `restore_page` | Restore, then place: caller placement, else stored property (best effort), else space root | Ready |
| `set_labels` | Add or remove page labels | Implemented locally; live acceptance pending |

Comment and attachment writes are not in the requirements and are not proposed here.

### Foundations shared by all tools

- Acceptance fixture setup script and live checks per [requirements §8](requirements.md#8-reliability-and-acceptance),
  mirrored by offline tests with synthetic fake responses.
- Remaining move, delete, trash and restore routes and adapters, including long-task polling,
  hierarchy caps, 409 conflicts and partial-failure results. Shared retry and uncertain-write
  behavior is implemented and has offline tests.

### Deferred review findings

P2 review findings left open when their PR was merged, for the final pass. Group related items into
design fixes rather than one patch each.

- PR #5, `service/writes.py`: label compensation can undo a label that another client added after the
  metadata snapshot, because Confluence's add-label response does not show whether this call caused
  the transition. Compensate only transitions this call is known to have caused.
- PR #5, `remote/confluence.py`: `list_drafts` can return a title-based `/display/…` or
  `resumedraft.action` URL that `PageLinkResolver` cannot resolve back to the draft. Return an
  ID-based supported URL, or resolve draft URLs.

## Decisions

- **Page text conversion:** whether to replace the hand-written HTML-to-text converter
  (`service/page_text.py`) with a thin Confluence layer over an off-the-shelf HTML-to-Markdown library,
  and restate D3 as faithful, readable text with documented limitations instead of a rule-by-rule
  specification.

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
  hyphens, up to 255 characters.
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
  Verify reverse compensation after definitive label failures and no compensation after uncertain outcomes.

### Other live checks

- Token lifecycle (expiry, revocation visibility) and concurrent processes once D1 is built.

Resource bundles expose UI code, not successful requests. Record dated results and limitations in the
research record.
