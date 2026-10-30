# Confluence Connector Roadmap

Pending implementation, decisions and live verification only. Requirements own behavior; architecture
owns design; research holds dated integration evidence. Finished work lives in Git/PR history.
Snapshot **2026-10-30**. Offline checks: 368 tests pass (one platform skip), compilation, pyflakes and
probe CLI help.

## Implementation

Items 5 and 6 follow the remaining priority the user set on 2026-10-30; 7 and 8 were decided or
found that day and have no rank yet.

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
- **Live check:** every setting round-trips without changing the others; restore the user's values.
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
removing page, space or tree watches), and live paging checks beyond 500 children and for tree/delete
preflight caps.

A whole-page cleaned-HTML read format is not implemented; `rich` remains verbatim Confluence storage
for editing. Teams HTML conversion is tracked in that connector's roadmap.

## Live verification

Open checks for implemented behavior; each feature above carries its own. Each check needs explicit
authorization for reads/writes and targets; synthetic pages moved to another space must be returned
and removed immediately. Use the [research toolkit](../research/README.md#synthetic-acceptance-fixture).

- **CQL timezone:** check repeated daylight-saving hours.
- **Search behavior:** check `siteSearch` ordering and `content.version.by` across supported Data Center
  versions; the read-only check on the configured instance passed (research §4.6).
- **Refused cross-space moves:** inspect terminal failure fields and whether the 10-second bound fits
  large subtrees. A pending outcome must never trigger compensating writes.
- **Comments:** on disposable synthetic content, verify inline body edits preserve anchors and
  whether inline resolution can be changed through REST. A second authorized test account is needed
  to verify the 403 for editing another user's comment.
- **Token lifecycle:** verify an expired PAT's response and cross-process sign-out with the bound
  credential format, without signing out the user's working credential.
- **Attachment upload size:** determine the configured instance's maximum upload size with a disposable
  synthetic attachment; normal-size create, version, metadata, trash and restore checks passed on
  2026-10-30 (research §6.4).
