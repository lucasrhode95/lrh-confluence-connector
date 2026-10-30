# Confluence Connector Roadmap

Pending implementation, decisions and live verification only. Requirements own behavior; architecture
owns design; research holds dated integration evidence. Finished work lives in Git/PR history.
Snapshot **2026-10-30**. Offline checks: 382 tests pass (one platform skip), compilation, pyflakes and
probe CLI help.

## Implementation

Item 8 was found on 2026-10-30 and has no rank yet.

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
- **Email settings:** the configured PAT read the settings form, but its save request returned 403.
  Verify independent round trips and restoration on an instance/account that permits this form write.
- **Token lifecycle:** verify an expired PAT's response and cross-process sign-out with the bound
  credential format, without signing out the user's working credential.
- **Attachment upload size:** determine the configured instance's maximum upload size with a disposable
  synthetic attachment; normal-size create, version, metadata, trash and restore checks passed on
  2026-10-30 (research §6.4).
