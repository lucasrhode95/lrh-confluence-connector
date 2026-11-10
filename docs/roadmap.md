# Confluence Connector Roadmap

Pending implementation, decisions and live verification only. Requirements own behavior; architecture
owns design; research holds dated integration evidence. Finished work lives in Git/PR history.
Snapshot **2026-11-10**. Offline checks: 401 tests (400 pass, one platform skip), compilation, pyflakes and
probe CLI help.

## Implementation

### Cut what Python 3.14 makes unnecessary

The project requires Python 3.14 since 2026-11-10. Review the code and remove what that leaves
unneeded:

- **Future imports:** `from __future__ import annotations` is in 72 files; 3.14 evaluates
  annotations lazily.
- **Old workarounds:** fallbacks or guards for standard-library behavior older Pythons had, for
  example `HTMLParser` raising on malformed markup.
- **Checks:** confirm compilation and pyflakes stay clean on 3.14 after the cleanup.
- **Reference:** the Teams connector did this in [lrh-teams-connector#51](https://github.com/lucasrhode95/lrh-teams-connector/pull/51).

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

- **CQL timezone:** check repeated daylight-saving hours on an instance whose server zone observes
  daylight saving; the configured instance's zone does not (research §4.4).
- **Search behavior:** check `siteSearch` ordering and `content.version.by` across supported Data Center
  versions; the read-only check on the configured instance passed (research §4.6).
- **Cross-space move failures:** a long task that finishes with `successful: false` has not been
  seen; title conflicts are refused before a task starts (research §6.3).
- **Comments:** a second authorized test account is needed to verify the 403 for editing another
  user's comment.
- **Token lifecycle:** verify an expired PAT's response and cross-process sign-out with the bound
  credential format, without signing out the user's working credential.
