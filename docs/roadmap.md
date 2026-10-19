# Confluence connector roadmap

Open work only. Settled requirements are in [requirements](requirements.md); the current implementation
is in [architecture](architecture.md); dated evidence is in [API research](api-research.md).
Delete items when complete. Snapshot **2026-10-19**.

Work is split by what it needs: **implementation** is code still to write; **decisions** wait for the
user; **live verification** checks something on the real instance before it is built or relied on.

## Implementation

### Proposed MCP tools

Names follow the sibling connectors (no product prefix; the MCP server name namespaces them). The
proof-of-concept tools have been removed. **Ready** means the evidence is enough to build it; otherwise
the blocking decision (D) or verification (V) is named. Every write tool also waits on V2, because
writes were verified only through a browser session.

| Tool | Purpose | Status |
|---|---|---|
| `get_pages` | Read pages by ID or URL as text with attachment/macro markers, comments per parse scope, and short version metadata | Ready |
| `download_attachments` | Save named attachments of a page locally; return paths | Ready |
| `export_pages` | Pages and versions plus parse scope into one ZIP with text and attachment folders | Ready |
| `create_page` | New page or new-page draft under a parent, in `rich` or `text` format | Ready |
| `update_page` | Replace title and/or whole body with an explicit `expected_version` | Ready |
| `publish_draft` | Publish a new-page draft, keeping its ID | Ready |
| `list_drafts` | The user's new-page drafts | Ready |
| `delete_draft` | Permanently delete a new-page draft (bypasses the trash) | Ready |
| `move_page` | Move to a target space, parent (or space root) and optional position, as one call | Ready |
| `delete_page` | Trash a page after a per-page `delete` check, with `children`: refuse (default), `move_up` or `trash`; reports former placement and a one-way warning | Ready |
| `list_trash` | Trashed pages per space or across visible spaces | Ready |
| `restore_page` | Restore, then place: caller placement, else stored property (best effort), else space root | Ready |
| `set_labels` | Add or remove page labels | Ready |

Comment and attachment writes are not in the requirements and are not proposed here.

### Foundations shared by all tools

- Acceptance fixture setup script and live checks per [requirements §8](requirements.md#8-reliability-and-acceptance),
  mirrored by offline tests with synthetic fake responses.
- Typed page and content models carrying source metadata and ordered references (page, version,
  attachment, comment, macro) mapped from REST JSON and storage/`view` structure before rendering.
- Remaining content routes and adapters for page text, attachments, exports and writes, including
  paging caps, CQL 400 details, 409 conflicts and Confluence 500 log referrals. The shared retry and uncertain-write policy is implemented and has
  offline tests; each later write capability still needs its own refusal and partial-failure coverage.

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
  `modified_after/before`, `parent`, `ancestor` and `include_archived`. Confirm these names and the
  UTC timestamp boundary behavior during user-run acceptance; requirements deferred exact names to
  implementation. It accepts up to 100 results per page, 50 values per collection filter and 255
  characters per filter value; query text is capped at 1,000 characters. Cursors are limited to
  16,384 characters and offsets to 1,000,000. These bounds protect request and response sizes.
- Quick-navigation title rows are mapped from the observed `contentNameMatches` envelope using
  `contentId/id`, `title/name`, `spaceKey` or nested `space`, and `url/href`. This route and row shape
  are browser-observed and deployment-specific; verify result coverage and returned fields on the
  configured instance. User lookup continues to use the CQL search and exact `/rest/api/user?key=`
  lookup for former users; quick navigation with `type=user` is known to fail.
- `get_page_tree` uses `rest/api/space/{spaceKey}/content/page` for space-root pages and
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
- `find_users` requires either a display-name `query` or exact `username`; the modes cannot be mixed.
  Name queries and usernames are capped at 255 characters, results at 100 per page and offsets at
  1,000,000. Verify current and former-user results during acceptance.

### Other live checks

- Token lifecycle (expiry, revocation visibility) and concurrent processes once D1 is built.

Resource bundles expose UI code, not successful requests. Record dated results and limitations in the
research record.
