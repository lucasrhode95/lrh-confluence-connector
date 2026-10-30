# Research tools

Scripts that ask Confluence what this instance actually allows and how its routes actually behave, and
an opt-in acceptance suite that checks the connector against synthetic pages. None of them runs as a
routine check, and none makes a request without an explicit flag.

Findings go to [`docs/api-research.md`](../docs/api-research.md), dated and anonymized. How to act on them
is in [architecture §6](../docs/architecture.md#6-capability-routing) (capability routing).

## What belongs in the findings

- Link to Atlassian's documentation for public routes, parameters and schemas. Do not copy that
  reference material here.
- Keep what Atlassian does not document or documents ambiguously: dated credential results, caps and
  paging behavior, search tokenization, write and trash side effects, failed variants and measured
  costs. A documented route returning 200 is not a finding.
- For private web routes, keep the request and response contract, the evidence source, the tested
  variants and the failure behavior. Separate browser and source observations from requests we sent.
- State the sample and what it does not prove. An empty search does not show a feature is unsupported,
  and one instance's behavior is not universal.

## Configuration

The probe and the acceptance scripts read the same private `confluence_config.json` as the connector,
through the same loader ([README](../README.md#setup), step 2). No instance, query or credential is
built in. `query` is the probe's search text, `form_auth_path` the route of the probe's form-login mode
and `endpoints.search_pages` its search route; the connector does not need them, and the probe stops
with their names when they are missing. `acceptance.personal_space_key` and
`acceptance.cross_space_key` are the only spaces the acceptance scripts may write to. The optional
`acceptance.cross_space_parent_id` is a page in the cross space that receives the cross-space move;
without it the page moves to that space's root. A move follows its parent's space, so the check reads
that page first and stops before any write unless it is a current page in the cross space.

## Authentication probe

```bash
uv run python research/probe_auth.py --config ./confluence_config.json --mode pat
```

One mode per run: `pat` (the connector's method), `anonymous`, `basic`, `form` (one login through the
undocumented web login, [research §2.2](../docs/api-research.md#22-credentials); `--remember-me` asks for
a persistent cookie) or `oauth` (a token from an administrator-registered app; no grant flow). Secrets
are prompted with `getpass`, never taken as arguments, printed or saved. The probe reads the current
user, both searches, the spaces and one content result, and prints statuses, response keys and counts.

## Synthetic acceptance fixture

The acceptance suite checks the connector, not Confluence, against pages with known facts
([requirements §12](../docs/requirements.md#12-acceptance)). Use a fresh run ID for each fixture.

| Step | Command | Changes Confluence? |
|---|---|---|
| 1 | `setup_acceptance.py --config … --run-id deadbeef` | No: lists what would be created |
| 2 | `setup_acceptance.py --config … --run-id deadbeef --apply` | **Yes**: creates or refreshes the `Connector Lab Acceptance` tree in the personal space |
| 3 | Add one inline comment to the root page in the Confluence UI | By hand |
| 4 | `run_acceptance.py --config … --run-id deadbeef` | No: lists the checks offline |
| 5 | `run_acceptance.py … --run-live` | No: reads, downloads and exports only |
| 6 | `run_acceptance.py … --run-live --allow-writes` | **Yes**: page, draft, label, move, trash and restore checks on scratch pages |

The automatic fixture omits the HTML macro because public rich writes reject it. HTML-macro rendering
has offline coverage; checking it live requires separately authorized manual fixture content. Move checks
reread metadata and complete sibling lists; delete/restore checks return their scratch pages to trash
and reread their status. Fixture body refresh rereads the version after moving a page.

Steps 2, 5 and 6 need the user's authorization for that run. Missing settings or manual steps make only
the dependent checks `skipped`. After an interrupted run, inspect the fixture before retrying. The runner
reports check names, statuses, timings and exception types, never response text or page content.


Disposable write checks clean confirmed creations even when an assertion fails: drafts are deleted,
current pages are trashed, and pages in a foreign space are returned first. `ScratchCleanupRequired`
means cleanup failed or stopped after an uncertain write/account change; inspect that fixture run
before retrying. The same-space move check verifies the version as well as parent and sibling order.

## Offline checks

```bash
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research tests
uv run pyflakes src research tests
uv run python research/probe_auth.py --help
```

Tests use synthetic settings and fake HTTP responses only. Each live acceptance area has offline
coverage:

| Acceptance area | Offline coverage |
|---|---|
| Fixture creation, opt-in flags and sanitized reporting | `test_acceptance_setup.py` |
| Search modes, scope, dates, cursors and quick navigation | `test_search.py`, `test_cql_dates.py`, `test_timezone_calibration.py` |
| Spaces, users, trees and metadata | `test_read_services.py`, `test_read_remote.py` |
| Text and rich reads, comments, typed references, versions, continuation | `test_page_text_render.py`, `test_pages_service.py`, `test_read_remote.py` |
| Attachment downloads, exports and local files | `test_attachments_service.py`, `test_exports_service.py`, `test_localfiles.py` |
| Page, draft and label writes, conflicts and content safeguards | `test_writes_service.py`, `test_write_remote.py`, `test_write_markup.py` |
| Moves, ordering, trash and restore | `test_structure_service.py`, `test_write_remote.py` |
| Retries, account isolation and concurrency | `test_remote.py`, `test_app.py`, `test_auth_store.py` |

## Output and safety rules

- Live requests, read or write, need the user's authorization for those requests and targets. Writes go
  only to synthetic pages in the configured spaces; nothing is purged.
- Output is statuses, counts, timings, response keys and error types: no tokens, cookies, page content,
  names or real IDs.
- Keep a permission denial, a malformed request, an unavailable route, an empty result and a transport
  failure apart when recording a result.

## Investigating the web UI

Probes test routes we already know how to call. To find a new route or request shape, watch what
Confluence's own UI does during one explicitly authorized, controlled action:

1. Sign in in a browser and perform **one** action with a unique marker, such as a search for
   `MARKER-1234`, or moving a named `Connector Lab` page.
2. Export the traffic (DevTools → Network → **Export HAR** with content) or the loaded resources, to a
   private folder **outside this repository**, and inspect it in place.
3. Record only routes, request and response shapes, key names and counts in the research record, then
   delete the export.

**Captures are credentials.** They contain session cookies, `atl_token` values, page content and names.
Never commit or share them, or turn them into test fixtures; re-implement what they show with the
connector's own token.
