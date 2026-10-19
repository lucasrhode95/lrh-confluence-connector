# Confluence connector roadmap

Open work only. Settled requirements are in [requirements](requirements.md); the current implementation
is in [architecture](architecture.md); dated evidence is in [API research](api-research.md).
Delete items when complete. Snapshot **2026-10-19**.

Work is split by what it needs: **implementation** is code still to write; **decisions** wait for the
user; **live verification** checks something on the real instance before it is built or relied on.

## Implementation

### Proposed MCP tools

Names follow the sibling connectors (no product prefix; the MCP server name namespaces them). The
three proof-of-concept tools (`confluence_search_pages`, `confluence_list_spaces`, `confluence_get_page`)
are replaced, not kept as aliases. **Ready** means the evidence is enough to build it; otherwise the
blocking decision (D) or verification (V) is named. Every write tool also waits on V2, because writes
were verified only through a browser session.

| Tool | Purpose | Status |
|---|---|---|
| `auth_status` | Configured instance, whether a token is stored, current user | Ready |
| `search_content` | Query plus `scope` (spaces, space type, content types, labels, creators/contributors, created/modified ranges, parent/ancestor, archived on by default), `match` and `fields` per [requirements §3](requirements.md#3-search-and-navigation); cursor paging | Ready |
| `find_users` | Resolve a current user's name to a username for creator/contributor filters; former users are findable only by username | Ready |
| `list_spaces` | Spaces by name, type and status, including archived | Ready |
| `get_page_tree` | Children (ordered, paged) and ancestors from a page or space root; depth-bounded | Ready |
| `get_pages` | Read pages by ID or URL as text with attachment/macro markers, comments per parse scope, and short version metadata | Ready |
| `get_page_metadata` | Fuller metadata for one or many pages: history, version list, labels, contributors, ancestors | Ready |
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

Logging in and out stay command-line subcommands (`confluence-connector login` with a hidden prompt,
`confluence-connector logout`), so a token never passes through an agent conversation. Comment and attachment writes are not
in the requirements and are not proposed here.

### Foundations shared by all tools

- Acceptance fixture setup script and live checks per [requirements §8](requirements.md#8-reliability-and-acceptance),
  mirrored by offline tests with synthetic fake responses.
- Typed models carrying source metadata and ordered content references (page, version, attachment,
  comment, macro) mapped from REST JSON and storage/`view` structure before any text is rendered.
- REST client covering the observed contracts: paging on `start + size` (never the echoed `limit` or
  `next` link), 500-item caps, 409 version conflicts, 400 CQL errors and 500 log referrals.
- Encrypted token storage with `msal-extensions` and the `login`, `logout`, `status` and `serve`
  subcommands ([architecture §3.1](architecture.md#31-token-storage-decided-not-built)); remove `main.py`.
- Retry and error policy from the shared rules: bounded read retries, writes retried only on proven
  non-application, uncertain writes reported. Offline tests with fake responses: connection reset then
  success, read timeout retried, write timeout reported as uncertain, persistent 500 reported after
  the retries, and 429 with `Retry-After`. Multi-step writes (page plus labels) report what was
  completed.

## Decisions

None open. Settled decisions are in [requirements](requirements.md).

## Live verification

Each check needs authorization for the exact requests. Synthetic `Connector Lab` fixture pages remain
in the personal space, including a rich-text sample page.

- Token lifecycle (expiry, revocation visibility) and concurrent processes once D1 is built.

Resource bundles expose UI code, not successful requests. Record dated results and limitations in the
research record.
