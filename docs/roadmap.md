# Confluence Connector Roadmap

Open work only. What the connector must do is in the [requirements](requirements.md); how it is built is
in the [architecture](architecture.md); dated evidence is in the [research record](api-research.md).
Items implemented in this pass remain here until the user reviews them.

Work is split by what it needs: **implementation** is code still to write; **decisions** wait for the
user; **live verification** checks built behavior against the real instance, and becomes implementation
only if a check fails.

Snapshot **2026-10-20**, from whole-project reviews of `e67b60d` and the unresolved review threads of
PRs #5–#9. An item that came from a review thread links it: resolve the thread, with a one-line reply
naming the fixing commit, when the fix lands.

Correctness pass **2026-10-20**, awaiting user review: the 18 original bug entries retain their original
context and now record implementation and offline coverage. The full suite ran 275 tests successfully
(one platform skip); compilation and the probe CLI help check passed. No live requests or review
requests were made. Broader refactoring, implementation of the decided move route and live verification remain open.

## Implementation

### Bugs

#### Calibration treats cross-field timestamp precision as an edit (P2)

- **Seen live, 2026-10-20:** search and REST modification instants differ by one millisecond. The
  stability guard compares their strings, retries and fails calibration despite a stable page.
- **Implemented, awaiting review:** use REST metadata for both initial and final timestamps, comparing
  parsed UTC instants. Search only selects the page. Synthetic offset tests now include the search/REST
  discrepancy and equivalent offset spellings; concurrent-edit tests still enforce retry and failure.
- **Live verification:** the corrected read-only calibration succeeded with offset zero. The operator
  subsequently persisted the setting through the CLI; date-only and timestamp filters passed live.


#### Acceptance view-file parameter has the wrong type (P2)

- **Seen live, 2026-10-20:** root creation returns HTTP 500 because the macro's `name` parameter is
  filename text. Confluence tries to cast the string to `AttachmentResourceIdentifier`.
- **Implemented, awaiting review:** the parameter now contains an `ri:attachment` element. A live
  isolation test verified that form, and the offline fixture/write-path test checks its exact shape.
  Full fixture setup and acceptance subsequently completed with the prerequisite skips listed below.


#### Repeated acceptance downloads mistake collision-safe names for missing content (P2)

- **Seen live, 2026-10-20:** the first download check passed; later runs failed when local filenames
  gained collision suffixes. The check keyed bytes by local filename instead of source identity.
- **Implemented, awaiting review:** match saved files to expected attachments by `attachment_id`,
  preserving source filename lookup from the API metadata. A synthetic regression uses renamed files.


#### Restore placement trusts a sparse success response (P2)

- **Seen live, 2026-10-20:** restore succeeds but returns no expanded space or version. Placement
  fails its current-space check, leaving the page at the root and failing acceptance.
- **Implemented, awaiting review:** reread current metadata after confirmed restoration before
  applying placement. An offline test uses a successful response with both fields omitted.


#### Unordered positions abort page reads (P1)

- **Seen live, 2026-10-20:** synthetic pages and expanded ancestors return `position: -1`; numeric
  count validation rejects it, aborting metadata, child reads and acceptance setup.
- **Implemented, awaiting review:** map the server's unordered-position sentinels to null in position
  fields only. An offline regression covers child rows and ancestor metadata. Other numeric fields
  retain their strict validation.


#### Personal-space keys reject account-name punctuation (P1)

- **Seen live, 2026-10-20:** the space listing identifies the account's personal space, but configuration
  and service validators reject its key because the username contains dots and `@`; setup stops before
  creating any fixture pages.
- **Implemented, awaiting review:** allow those characters after the `~` personal-space prefix in the
  configuration, tree, page-write and structure boundaries. Existing synthetic boundary tests now use
  a fictional dotted account name. Normal-space validation remains unchanged.


Each bug is **Seen** (a reproduction or explicitly identified inspection finding), **Expected**, **Cause** and
**Fix**. Priority follows the review scale: P1 breaks a promised behavior, P2 is a narrower defect, P3 is
minor.

#### Subtree reads stop at the first short page (P1)

- **Seen:** `delete_page(page="4242", children="trash")` on a page whose children span two server pages
  reports every page trashed, while the descendants on the second page stay current.
- **Expected:** every descendant is read before the first write, or the call stops without writing.
- **Cause:** child-page mapping drops `_links.next`, and traversal treats any page shorter than the
  requested 500 as the last ([`service/structure.py`](../src/confluence_connector/service/structure.py),
  [`remote/confluence.py`](../src/confluence_connector/remote/confluence.py)). The space listing makes the
  same assumption.
- **Fix:** keep the continuation signal and advance on the returned `start + size`. Both listings send
  `_links.next` only while more results exist ([research §5.2](api-research.md#52-paging)).
- **Implemented — awaiting review (2026-10-20):** Preserved the remote continuation signal for
  content pages; trees and delete preflight use it and advance by actual `start + size`, without
  following next URLs. Space listing does the same. Offline regressions cover short pages without
  totals, wrong next URLs, and reading every descendant before the first trash write.

#### Moving to the space root does nothing (P1)

- **Seen:** `move_page(page="4242", space_key="LAB")` returns `moved: true`, but the page keeps its
  parent and version. `restore_page` placement and `delete_page(children="move_up")` under a root page
  hit the same path.
- **Expected:** the page is a top-level page of the space.
- **Cause:** a content update with `ancestors: []` is accepted with 200 and ignored
  ([research §6.3](api-research.md#63-moves)).
- **Fix:** move to the root with `movepage.action` and `position=topLevel`, which was verified, and change
  the test that asserts the empty ancestor list.
- **Implemented — awaiting review (2026-10-20):** Root placement, `move_up` to a root, and restore
  root recovery now use the verified move action; the content-update adapter refuses an empty-parent
  update. A root action failure no longer reports its parent change as applied. Non-root moves
  retain D7’s current route policy. Offline tests cover root actions and restore recovery.

#### Every live acceptance run fails (P1)

- **Seen:** `run_acceptance.py --run-live` records `authentication` and `fixture` as failed with
  `TypeError` and exits 1.
- **Expected:** both checks pass.
- **Cause:** `report.run("authentication", _pass())` passes a coroutine where `AcceptanceReport.run`
  expects a callable ([`research/run_acceptance.py`](../research/run_acceptance.py)).
- **Fix:** pass `_pass`. Review thread:
  [PR #8](https://github.com/lucasrhode95/lrh-confluence-connector/pull/8#discussion_r4328507237).
- **Implemented — awaiting review (2026-10-20):** Passed `_pass` as a callable for authentication
  and fixture checks. An offline runner test executes both prerequisites and verifies PASS outcomes.

#### `move_up` undoes a concurrent reparent (P2)

- **Seen:** while `delete_page(children="move_up")` runs, another user moves child `B` under page `X`;
  the connector moves `B` back under the deleted page's parent.
- **Expected:** `B` stays where the other user put it, reported as skipped.
- **Cause:** each child's fresh metadata is checked only for being current, not for still having the
  captured parent and space.
- **Fix:** skip a child whose parent or space changed. Review thread:
  [PR #7](https://github.com/lucasrhode95/lrh-confluence-connector/pull/7#discussion_r4328234270).
- **Implemented — awaiting review (2026-10-20):** Fresh child metadata must still match the captured
  parent and space. Changed children receive `skipped` and are left alone. Offline cases cover both
  parent and space changes.

#### `restore_page` refuses display URLs (P2)

- **Seen:** `restore_page(page="https://wiki.example.com/display/LAB/Old+Plan")` for a trashed page
  fails with `NotFound`.
- **Expected:** the trashed page is found and restored, as with its numeric ID.
- **Cause:** display URLs resolve through a CQL title search, and CQL leaves out trashed content.
- **Fix:** for `restore_page`, resolve display URLs with
  `GET /rest/api/content?type=page&spaceKey=…&title=…&status=trashed`, which was verified
  ([research §6.4](api-research.md#64-trash-and-restore)). Review thread:
  [PR #7](https://github.com/lucasrhode95/lrh-confluence-connector/pull/7#discussion_r4328234259).
- **Implemented — awaiting review (2026-10-20):** Added a trash resolution scope, carried through
  short-link redirects. Display URLs use the content collection with `status=trashed`, space and
  exact title. An offline route test checks the decoded title, scope and resolved ID.

#### Punctuated search terms match either half (P2)

- **Seen:** `search_content(query="ci-cd")` returns pages that mention only `ci` or only `cd`: 2,160
  hits on the tested instance, against 482 for the quoted term.
- **Expected:** `ci-cd` is matched as one term.
- **Cause:** the term is sent unquoted, and the search engine splits it at the hyphen
  ([research §4.2](api-research.md#42-terms-and-punctuation)).
- **Fix:** quote terms with inner punctuation in `match: "all"` and `"any"`
  ([`service/search.py`](../src/confluence_connector/service/search.py)).
- **Implemented — awaiting review (2026-10-20):** Wrapped inner-punctuation terms in text-query
  quotes for `all` and `any`, retaining separate Lucene and CQL escaping. Tests cover both modes and
  metacharacters; the existing title normalization contract is unchanged.

#### External links can take an attachment's identity (P2)

- **Seen:** a page links `https://files.example.org/download/attachments/100/report.pdf`, and the page also has an attachment
  `report.pdf`; `get_pages` returns a `[File: report.pdf]` reference carrying the local attachment ID.
- **Expected:** the external link stays a link. Attachment identities and owner pages come from
  verified source metadata, including references to attachments of other pages.
- **Cause:** attachment classification and filename matching run before the link's origin is
  checked; attachments of included pages are ambiguous the same way.
- **Fix:** validate origin and owner page first
  ([`service/page_text.py`](../src/confluence_connector/service/page_text.py)).
- **Implemented — awaiting review (2026-10-20):** Checked instance origin before attachment
  classification. Local filename lookup requires the source owner to be the current page and cannot
  override an explicit attachment ID. Included content does not inherit local ownership; source
  container IDs and download paths retain foreign owners and URLs. Synthetic tests use colliding
  filenames across hosts/pages.

#### Page text loses identity in four edge cases (P2)

All four in [`service/page_text.py`](../src/confluence_connector/service/page_text.py), from PR #6:

- **Jira without a storage key.** Seen: with `macros_from_storage=false`, or inside an included page,
  rendered Jira content becomes `[Unrendered macro: jira]`. Fix: keep the rendered text when no key is
  known. [Thread](https://github.com/lucasrhode95/lrh-confluence-connector/pull/6#discussion_r4327881306).
- **Image filename attribute.** Seen: an image carrying only `data-linked-resource-filename` reads as
  `[Image]`. Fix: read that attribute.
  [Thread](https://github.com/lucasrhode95/lrh-confluence-connector/pull/6#discussion_r4327881317).
- **`data-page-id` links.** Seen: a page link whose ID is only in `data-page-id` has no page reference.
  Fix: read the attribute.
  [Thread](https://github.com/lucasrhode95/lrh-confluence-connector/pull/6#discussion_r4327881324).
- **Fence-like code lines.** Seen: a code line starting with ```` ``` ```` or `~~~` splits one code block
  into two budget blocks. Fix: split blocks on the parsed structure, not on fence text.
  [Thread](https://github.com/lucasrhode95/lrh-confluence-connector/pull/6#discussion_r4327881330).
- **Implemented — awaiting review (2026-10-20):** Preserved rendered Jira text when no storage key
  exists; read image filename and page-ID attributes. Code fences are sized from parsed code
  contents, and budget splitting recognizes matching delimiters rather than toggling on any
  fence-like line. Golden output and focused regressions cover these cases, including embedded
  backticks and tildes.

#### Calibration measures an edit as an offset (P2)

- **Seen:** the sample page is edited during `auth`; a UTC instance is written as
  `"cql_timezone": "UTC+00:01"`.
- **Expected:** calibration measures the instance, or stops without writing.
- **Cause:** the binary search assumes the sample's modification time does not change.
- **Fix:** re-read the sample's timestamp afterwards and retry once or abandon on change
  ([`service/timezone_calibration.py`](../src/confluence_connector/service/timezone_calibration.py)).
- **Implemented — awaiting review (2026-10-20):** Re-read the exact sample’s modification timestamp
  after the binary search. A changed or unavailable sample retries once; repeated change fails
  without returning an offset to persist. Offline tests cover a successful retry and bounded
  abandonment.

#### A malformed credential blocks sign-out (P2)

- **Seen:** after the stored record is damaged, `auth --sign-out` fails with the same decoding error
  as every other command.
- **Expected:** sign-out clears the record.
- **Cause:** sign-out decodes the record before clearing it
  ([`auth/store.py`](../src/confluence_connector/auth/store.py)).
- **Fix:** clear without decoding, keeping encryption and the lock.
- **Implemented — awaiting review (2026-10-20):** Sign-out checks the encrypted record’s presence
  without decoding its JSON, then clears it under the existing lock. Reads/sign-in still fail closed
  on malformed records. The regression exercises malformed load, explicit sign-out and subsequent
  empty load.

#### A malformed URL aborts a batch (P2)

- **Seen:** `get_page_metadata(pages=["4242", "https://[broken"])` fails as a whole with `ValueError`.
- **Expected:** `4242` is returned and the URL is listed as an invalid request.
- **Cause:** `urlsplit` raises before validation
  ([`remote/links.py`](../src/confluence_connector/remote/links.py)).
- **Fix:** convert the error to `InvalidRequest`.
- **Implemented — awaiting review (2026-10-20):** Converted URL parsing failures to
  `InvalidRequest`. An offline metadata batch returns its valid page and a per-reference failure for
  the malformed URL.

#### Acceptance setup cannot create its fixture (P2)

- **Seen:** `setup_acceptance.py --apply` on an empty space stops with `InvalidRequest` before
  creating the root page.
- **Expected:** the fixture tree is created.
- **Cause:** the root body includes the HTML macro, which the write service refuses by design
  ([`research/acceptance_fixtures.py`](../research/acceptance_fixtures.py)).
- **Fix:** drop the HTML macro from the fixture, or add it through a fixture-only path. Review thread:
  [PR #8](https://github.com/lucasrhode95/lrh-confluence-connector/pull/8#discussion_r4328507219).
- **Implemented — awaiting review (2026-10-20):** Removed the HTML macro from the automatically
  written fixture; product write validation remains strict. Replaced the shallow macro-list test
  with setup through the real write service and fake HTTP transport. Setup also rereads the version
  after placement changes before a body update, with its own regression. HTML-macro behavior remains
  an offline renderer case and a separately authorized live verification.

#### The live move check ignores order (P2)

- **Seen:** the move check passes when the parent changed but the requested position did not.
- **Expected:** the check fails unless the parent and the sibling order both match.
- **Cause:** it reads the returned parent only, and the cross-space check trusts the service's
  reported target ([`research/run_acceptance.py`](../research/run_acceptance.py)).
- **Fix:** re-read metadata and ordered children independently before cleanup.
- **Implemented — awaiting review (2026-10-20):** Required confirmed positioning and independently
  reread metadata and all ordered children for same-space and cross-space moves. Tests reject
  explicit positioning failure, wrong sibling order and wrong destination.

#### Acceptance leaves restored pages current (P2)

- **Seen:** after a passing `--allow-writes` run, the `move_up` parent and the three subtree pages are
  current pages in the personal space; a second run with the same run ID fails on duplicate titles.
- **Expected:** every scratch page ends in the trash.
- **Cause:** the restore checks never trash the pages again.
- **Fix:** trash them after their assertions. Review thread:
  [PR #8](https://github.com/lucasrhode95/lrh-confluence-connector/pull/8#discussion_r4328507255).
- **Implemented — awaiting review (2026-10-20):** Re-trashed the restored move-up parent and
  subtree, then independently checked that all seven delete/restore scratch pages are trashed. An
  offline workflow runs the production structure service against synthetic state and verifies every
  scratch page’s final status.

#### Label removal is skipped beyond 200 labels (P3)

- **Seen:** `set_labels(page="4242", remove=["old"])` on a page with 250 labels reports `old` as
  unchanged, though it is the 230th label.
- **Expected:** the label is removed.
- **Cause:** metadata reads only the first labels page, which holds 200
  ([research §5.3](api-research.md#53-labels)).
- **Fix:** follow `_links.next` on `/label` before deciding a label is absent.
- **Implemented — awaiting review (2026-10-20):** When the metadata label expansion has
  continuation, read the full label collection using validated offsets and bounded paging before
  computing changes. An integration regression removes a label absent from the expansion, using
  actual returned sizes rather than the next URL offset.

#### Definite failures are reported as `not_attempted` (P3)

- **Seen:** a `delete_page` step that Confluence refused with 403 is reported as `not_attempted`.
- **Expected:** `failed`, keeping `unknown` for uncertain outcomes and `not_attempted` for later steps.
- **Fix:** add the status ([`service/structure.py`](../src/confluence_connector/service/structure.py)).
- **Implemented — awaiting review (2026-10-20):** Added `failed` for definite operation errors,
  keeping `unknown` for uncertain outcomes and `not_attempted` for subsequent steps. Updated domain
  output and partial-failure tests.

#### The timezone hint omits the restart (P3)

- **Seen:** a timestamp filter without `cql_timezone` says to run `status --check`; after that the same
  call still fails.
- **Cause:** MCP processes cache the configuration.
- **Fix:** add "then restart the MCP client" to the message
  ([`service/cql_dates.py`](../src/confluence_connector/service/cql_dates.py)).
- **Implemented — awaiting review (2026-10-20):** The missing-timezone error now tells the user to
  calibrate and restart the MCP client.

#### Single-file exports lose owner-only permissions on POSIX (P3)

- **Seen by inspection:** a staging file created under a typical POSIX umask can leave a single-page
  export at `0644`; this was not reproduced on Linux.
- **Expected:** `0600`, like every other output file.
- **Cause:** the claimed file is replaced by a staging file written under the normal umask
  ([`service/exports.py`](../src/confluence_connector/service/exports.py)).
- **Fix:** write the staging file with owner-only permissions.
- **Implemented — awaiting review (2026-10-20):** Created staging files exclusively with mode
  `0600`, so atomic replacement preserves owner-only file permissions. The single-export test
  asserts mode on POSIX; this Windows run verifies bytes/layout but cannot execute that platform
  assertion.

### One route for same-space moves

**Decided 2026-10-20; implemented, awaiting review (2026-10-21).** Same-space reparenting and
positioning now use one `movepage.action` request. `move_up` and restore placement use that route too;
the version-creating parent update was removed. Definite refusals retain the previous parent, while
uncertain results leave placement unknown and advise inspection before retry. Root `last` placement
uses `below` the final root page because `topLevel` inserts first. Offline tests verify one action,
unchanged versions, order-preserving `move_up`, and definite and uncertain failures. Architecture and
requirements now describe the route. Live acceptance must independently verify parent, order and
unchanged version ([research §6.3](api-research.md#63-moves)).

### Improvements

Working behavior that could be simpler, cheaper or better tested.

#### One validator for each input kind

Page references, space keys, the current-page check, the account-error passthrough, filename
sanitization (attachments and exports), `_cql_literal` and same-instance URL parsing are repeated across
[`remote/links.py`](../src/confluence_connector/remote/links.py),
[`remote/confluence.py`](../src/confluence_connector/remote/confluence.py) and the services, and already
differ: one copy refuses a control character that another accepts. Keep one of each.

#### One placement resolver

`create_page`, `move_page` and `restore_page` resolve `parent_id`, `space_key`, `position` and `sibling`
through two implementations (`_Placement`/`_create_placement` and `PagePlacement`/`_resolve_placement`).
Share one.

#### Rich-body checks on BeautifulSoup

[`service/write_markup.py`](../src/confluence_connector/service/write_markup.py) is a 362-line
hand-written `HTMLParser`. Rebuild it on BeautifulSoup, already a dependency, as was done for page text.

#### Parse storage once per read

Storage is parsed separately for macros and for references, and a `rich` read still runs the Markdown
conversion it then discards. Parse once and skip the conversion for `rich`.

#### One retry loop in the transport

`ConfluenceTransport.download_to` repeats the retry loop of `_exchange`. Merge them.

#### Remove dead code and add an undefined-name check

Remove the unused helper in `service/writes.py`, the unreachable ownership branch in `bootstrap.py` and
the needless `async` on `_nodes_for_children`. Add a `pyflakes`-style check to the documented checks:
`service/pages.py` raised `Upstream` without importing it, which `compileall` does not catch.
The missing import is fixed in this pass; the dead-code cleanup and additional static check remain open.

#### Fewer, stronger tests

- Run fixture setup through its real write path against a fake transport, instead of the tests that
  only list macro names; it would have caught the fixture bug above.
- Make tables of near-identical cases: uncertain-write outcomes and account changes after a write
  (`test_remote.py`); label partial failures and draft cursors (`test_writes_service.py`); config
  precedence (`test_config.py`); MCP round-trips (`test_app.py`, moving the export attachment-reuse
  case to the export tests).
- Remove the trash assertions repeated in the omnibus route test and assertions on embedded constants,
  such as the fixture PNG signature.

The shallow fixture macro-list test and embedded PNG-signature assertion were removed in this pass;
fixture creation now has an integration regression. The remaining test consolidation is still open.

### Enhancements

None open. Version comparisons, activity feeds and attachment interpretation are outside the current
requirements ([requirements §4](requirements.md#4-non-goals)).

## Decisions

None open. The same-space move route is decided and tracked under implementation.

## Live verification

Built behavior not yet checked on the configured instance. Each check needs the user's authorization
for the reads or writes it makes; record the dated result in the research record. Synthetic
`Connector Lab` pages stay in the personal space for these checks.

### Acceptance run

**Live run completed 2026-10-20: 13 passed, 0 failed, 4 skipped.** Setup created four synthetic fixture
pages, two attachments and two comments, and applied the fixture label in the personal space.
The final run used `--run-live --allow-writes`; it passed search and non-date filters, paging/user
resolution, trees and metadata, text/rich and historical reads, downloads/exports, concurrent reads,
page/draft/label writes, move/order, and all delete/restore scenarios. Scratch pages were returned to
trash; the four reusable fixture pages remain for later checks.

Live execution found and fixed personal-space key punctuation, the malformed `view-file` attachment
parameter, unordered position sentinels, sparse restore responses and the repeated-download check's
use of local filenames instead of source IDs. Fixes and regressions remain above for user review.
The earlier HTTP 500 was the fixture parameter's server-side `ClassCastException`; it was not an
authentication failure. Offline checks after these fixes: 278 tests, one platform skip, compilation.

**Targeted live follow-up, 2026-10-20:** after the operator saved `cql_timezone`, date-only filters,
timestamp intervals supplied with offsets, and an excluding timestamp bound all passed. A separate
synthetic page was moved under the explicitly authorized parent in another space, verified there,
and immediately moved back under the personal fixture root. The round trip took 3.82 seconds and
preserved its version. The scratch page was trashed in the personal space; direct child-list and
metadata reads confirmed no test page remained at the authorized destination. No destination setting
was persisted, and no real target identifiers are retained here.

The date and cross-space skips from the full run are now covered by this targeted follow-up. Still
needed: add one inline comment to the fixture root in the UI, then rerun marker and content-loss checks.
The standalone runner's optional cross-space setting remains unset; any future run must use an
explicitly authorized destination and the same immediate return/cleanup policy.

See the [research toolkit](../research/README.md#synthetic-acceptance-fixture) for the opt-in commands.

### Archived spaces

`list_spaces` requests `status=current` and `status=archived` separately; Atlassian documents the
parameter but not its values. Check that both lists are complete on the instance, for example against
the space directory's archived filter.

### CQL time zone

Calibration measured `+00:00`, while both the profile and the instance zone were UTC, so it cannot tell
which zone CQL uses ([research §4.4](api-research.md#44-dates-and-time-zones)). On an account whose
profile zone differs from the server's, check which one the measurement follows, and check that a
search inside a repeated daylight-saving hour includes both occurrences.

### Quick-navigation titles

Title search merges the private quick-navigation route on the first page. Check its coverage and fields
on another space type (personal, archived) and a title with `"`, which CQL `title =` cannot match.

### Very long child lists

No parent with more than 500 children was found, so the 500-per-request cap of `child/page` without
expansion is untested ([research §5.2](api-research.md#52-paging)). If one exists, check paging past
500, and that the 5,000-page read cap for `delete_page` and the 1,000-page tree cap are practical.

### HTML macros and includes

The HTML macro reaches `view` with no wrapper, so its position cannot be tied to its storage entry; the
connector marks it after the body. Check whether any server field maps it to a position, and whether
macros inside resolved includes keep a stable identity.

### Cross-space move failures

The success path of the long task is verified. Check a refused cross-space move (for example to a space
without create permission): the terminal long-task fields, the error text, and whether 10 seconds is
enough on a large subtree.

### Token lifecycle

Check what an expired and a revoked PAT return (status, body), so `AuthenticationRequired` can say which
it is, and that two MCP processes see a sign-out on their next call.

### Write bounds

Check that the input bounds fit real content: 2,000,000 characters per body, 255-character titles and
labels, and whether duplicate-title preflight should also see the user's hidden new-page drafts.
