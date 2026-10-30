# Confluence Connector Roadmap

Pending implementation, decisions and live verification only. Requirements own behavior; architecture
owns design; research holds dated integration evidence. Finished work lives in Git/PR history.
Snapshot **2026-10-30**. Offline checks: 305 tests pass (one platform skip), compilation, pyflakes and
probe CLI help.

## Implementation

Sections 1–6 follow the priority the user set on 2026-10-30; 7 and 8 were decided or found the same
day and have no rank yet.

### 1. File-based page edits

Approved by the user on 2026-10-28. The rich round trip exists (`get_pages` and `export_pages` return
the unchanged storage body; `update_page` accepts it), but the write body travels inline. On
2026-10-28 an agent needed to add one item to a 46,000-character page: the rich read was too large for
its context, and `update_page` would have needed the whole body retyped in the call, so it used the
REST API from the browser instead. Partial-edit parameters (paths, block IDs, anchors) were rejected:
they break when the page changes around them.

- **Read:** `export_pages` with `format: "rich"` and `parse_scope.comments: false` already writes the
  unchanged storage body to a file, after a leading header comment with the page ID and version.
- **Write:** `update_page` accepts `body_file`, a rich export file, in place of `body`. The connector
  strips its own header comment and takes the page ID and version from it as `expected_version`; an
  explicit `page` or `expected_version` must match. Refuse a file without the header, from a different
  page, from a `text` export, or with the appended comments section. `create_page` accepts a plain
  storage file as `body_file`.
- **No-op round trip:** exporting and immediately updating with no edits must leave the stored body
  byte-identical, including inline `<code>`, code and expand macros, status lozenges, anchors, emoji
  and tables. Apply the `<pre>`/`<code>` conversion only where it cannot change existing storage.
- **Safety:** the existing stale-version, inline-comment-loss and removed-macro checks still apply.
  No connector size cap (see 7).
- **Server instructions:** ship them with the feature, not before. Proposed text: "To edit a page,
  export it with `export_pages` (`format: "rich"`, comments off), change the file with exact
  find-and-replace, then pass it to `update_page` as `body_file`. Never retype a large body inline or
  rebuild it from text output."
- **Docs and tests:** synthetic tests for the no-op round trip, an edited file, a stale version, each
  refusal and `create_page` from a file; update requirements §9–§10, architecture and README.

### 2. Attachment management

Approved by the user on 2026-10-30: full control over attachments. Reads exist (`get_pages` lists
attachments; `download_attachments` fetches bytes). Writes are missing: on 2026-10-26 a doc refactor
needed upload, new versions and removal, and used one-off scripts over `ConfluenceTransport` for all
three.

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
- **Preflight:** files exist and are readable, media type from the extension, no duplicate filenames in
  one call. No connector size cap; report the instance's own size refusal.
- **SVG images:** upload with `image/svg+xml`, then embed with
  `<ac:image><ri:attachment ri:filename="…"/></ac:image>`, which renders inline; the `view-file` macro
  that drag-and-drop inserts shows a file icon, and inline `<svg>` is stripped (research §6.5).
- **Server instructions:** ship them with upload. Remind agents that they can show images inline,
  SVG included, and how. Proposed text: "To show an image inline (PNG, JPEG, GIF or SVG), upload it
  with `upload_attachments`, then reference it in the page body as
  `<ac:image><ri:attachment ri:filename="name.svg"/></ac:image>`. Do not use the `view-file` macro or
  a raw `<svg>` element: the first shows a file icon for SVG, and Confluence strips the second." The
  write tool descriptions carry the same `ac:image` example.
- **Result:** per attachment, ID, version and download URL, or the failure. No rollback of
  attachments already changed.
- **Docs and tests:** synthetic tests for create, new version, rename, trash, restore, refusals and
  uncertain outcomes; remove attachment writes from the requirements §4 non-goals; update
  requirements, architecture and README.

### 3. Notify watchers flag

Requested by the user on 2026-10-30, for minor fixes that should not email every watcher. Every
connector edit notifies watchers today: `update_page` never sends `version.minorEdit`. The editor's
"Notify watchers" checkbox is exactly that field, and leaving it unset suppresses the edit email
(research §6.1).

- **Flag:** one optional `notify_watchers` flag, default `true`, on every write tool (decided
  2026-10-30). `false` sends `version.minorEdit: true` wherever the request creates a version. For
  writes without a version field (moves, trash, restore, labels), find whether Confluence offers a
  way to suppress their notifications; where it does not, say so in the tool description and result.
- **Result:** echo the flag sent. Confluence reads back `minorEdit: false` either way, so the result
  cannot confirm it.
- **Docs and tests:** synthetic tests that the field is sent only for `false`; update requirements
  §10, architecture and the tool descriptions.

### 4. Comment management

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
- **Delete:** `delete_comment`, allowed even if Confluence deletes permanently (decided 2026-10-30).
  The tool description and server instructions must say that deletion is destructive and, if
  confirmed live, cannot be undone; require an explicit confirmation flag.
- **Safety:** account check on every write; when a page reference is given, the comment must belong to
  it. Surface 403 as a permission refusal. Never resend a write whose outcome is uncertain.
- **Result:** comment ID, version, page and URL, or the failure.
- **Docs and tests:** synthetic tests for create, reply, update, stale version, resolve, delete,
  refusals and uncertain outcomes; remove comment writes from the requirements §4 non-goals; update
  architecture and README.

### 5. Edited macros reported as removed

On 2026-10-28 an `update_page` that changed eight status lozenges from one title and colour to
another warned "8 status macro(s) would be removed". Nothing was removed: the stored page had the same
31 status macros before and after. `_macro_identity` in `service/write_markup.py` keys a macro on its
name plus every parameter, so any parameter edit counts as one removal and one addition.

- **Identity:** match macros by `ac:macro-id` when both bodies carry it; fall back to name and
  parameters only for macros without one.
- **Warnings:** report removed macros (ID gone) separately from changed ones (same ID, different
  parameters or body), e.g. "8 status macro(s) changed". A changed macro is not a loss.
- **Tests:** synthetic regressions for a parameter edit (changed, not removed), a real removal, a
  macro without an ID, and an ID moved to a different macro name.

### 6. Email notification settings

Requested by the user on 2026-10-30: read and change the signed-in user's email notification
settings (profile > Settings > Email), such as autowatch and notifications for one's own actions. The
user approved the undocumented UI form for this.

- **Isolation:** keep the undocumented form access in its own user-settings module, separate from the
  REST adapter, so the rest of the connector never depends on it.
- **Route:** the UI reads `users/viewmyemailsettings.action` and saves a form to
  `users/editmyemailsettings.action` with an `atl_token`; confirm how a PAT obtains that token.
- **Settings:** the eight on the page: autowatch, daily updates, all blog posts, network, new
  followers, notify on my actions, show changed content and recommended updates.
- **Safety:** the edit form does not always show stored values. On 2026-10-30 it showed "Subscribe to
  recommended updates" checked while the stored value was off, so saving one change also turned that
  on. Read stored values from the view page, send the complete set with only the requested changes,
  then reread and report any setting that differs.
- **Docs and tests:** synthetic form fixtures; this changes account configuration, so the tool
  description must say so.

### 7. Write input handling

Decided by the user on 2026-10-30.

- **No body cap:** remove `MAX_WRITE_BODY_CHARS` and the 2,000,000-character `max_length` on the
  `create_page` and `update_page` bodies; no functionality needs them. A 5,000,000-character body saved
  live (research §6.1). Update requirements §10.
- **Send malformed rich markup as is:** stop letting BeautifulSoup repair unparseable input in
  `write_markup.to_storage`. Keep the targeted conversions and the active-content refusals, but when
  the input is not well formed, send it unchanged and return Confluence's 400 with its row and column.
  Update requirements §10.1 and the tool descriptions.

### 8. Macros inside included pages

Found live on 2026-10-30 (research §5.1). Text output renders an included page's content, including
its macro markers, but typed references list only the include itself: macro identities come from the
host's storage. An included HTML macro is missed entirely, because its `view` output has no wrapper
and only the included page's storage names it.

- **Identity:** resolve each `include` and `excerpt-include` target, read its storage once, and add its
  macro identities to the host's references, marked with the page they came from.
- **Bounds:** follow nested includes to a fixed depth and stop on cycles; report includes that could
  not be read.
- **Tests:** synthetic host and target pages covering status, expand and HTML macros, nesting and a
  cycle.

## Decisions

No decisions are open. Not planned, decided 2026-10-30: watch management (listing, adding or
removing page, space or tree watches).

A whole-page cleaned-HTML read format is not implemented; `rich` remains verbatim Confluence storage
for editing. Teams HTML conversion is tracked in that connector's roadmap.

## Live verification

Each check needs explicit authorization for reads/writes and targets; synthetic pages moved to
another space must be returned and removed immediately. Use the
[research toolkit](../research/README.md#synthetic-acceptance-fixture).

On 2026-10-30 the acceptance suite passed with an inline comment in the fixture (16 passed; the
cross-space check was run separately under an authorized parent page and passed). The same day
settled: the archived space list against the UI, quick-navigation coverage, CQL's zone, revoked
tokens, write bounds, and HTML macros inside includes (research §2.2, §4.1, §4.3, §4.4, §5.1, §6.1,
§6.3). Client setup passed in Claude and Codex: after a restart, each discovered the tools and ran
`auth_status` and an approved page-metadata read.

- **CQL timezone:** check repeated daylight-saving hours.
- **Search behavior:** check `siteSearch` ordering and `content.version.by` across supported Data Center
  versions; the read-only check on the configured instance passed (research §4.6).
- **Refused cross-space moves:** inspect terminal failure fields and whether the 10-second bound fits
  large subtrees. A pending outcome must never trigger compensating writes.
- **Token lifecycle:** verify an expired PAT's response and cross-process sign-out with the bound
  credential format, without signing out the user's working credential.
- **File-based page edits:** once implemented, export a disposable fixture page with macros,
  lozenges, tables and inline code, update it from the unedited file, and confirm the stored body and
  rendered page are unchanged apart from the version.
- **Attachment management:** once implemented, check in a disposable fixture page the instance's size
  limit, new-version behavior on an existing filename, SVG/PNG media types, rename, trash and restore,
  and what decides the attachment-delete permission refused on 2026-10-26.
- **Notify watchers flag:** once implemented, confirm on disposable fixture pages that `false` sends no
  edit email and `true` does, and which version-less writes can suppress notifications. Use "Notify on
  my actions" with the user's approval, and restore it.
- **Comment management:** once implemented, check in a disposable fixture page whether deleting a
  comment trashes or purges it, whether inline comments can be updated and resolved through REST,
  what happens to replies when a parent is deleted, and editing another user's comment (expect 403).
- **Email settings:** once implemented, check that every setting round-trips without changing the
  others.
- **Parked:** paging beyond 500 children and tree/delete preflight caps; judged unnecessary for now on
  2026-10-30.
