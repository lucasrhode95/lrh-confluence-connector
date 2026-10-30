# Confluence API research

What Atlassian does not document about the Confluence Data Center routes this connector uses, or
documents too vaguely to build on without testing: page caps and paging, search tokenization, CQL dates,
how writes, moves, trash and restore really behave, and the private web routes the connector relies on.
Documented behavior is not repeated here; the
[Data Center REST reference](https://developer.atlassian.com/server/confluence/rest/latest/) and the
[CQL guide](https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/) are the
reference for routes, parameters and schemas.

**Product scope:** [requirements](requirements.md) · **How it is built:** [architecture](architecture.md) ·
**Probes:** [`research/`](../research/README.md)

## Contents

1. [Summary](#1-summary)
2. [Instance and credentials](#2-instance-and-credentials): [instance](#21-instance),
   [credentials](#22-credentials)
3. [Transport](#3-transport)
4. [Search](#4-search): [endpoints and archived spaces](#41-endpoints-and-archived-spaces),
   [terms and punctuation](#42-terms-and-punctuation), [titles](#43-titles),
   [dates and time zones](#44-dates-and-time-zones), [users](#45-users)
5. [Reading pages](#5-reading-pages): [bodies and macros](#51-bodies-and-macros), [paging](#52-paging),
   [labels](#53-labels), [attachments, comments and versions](#54-attachments-comments-and-versions)
6. [Writes](#6-writes): [pages and versions](#61-pages-and-versions), [drafts](#62-drafts),
   [moves](#63-moves), [trash and restore](#64-trash-and-restore),
   [rich-text input](#65-rich-text-input)
7. [Private routes seen but not used](#7-private-routes-seen-but-not-used)

**How to read it.** Every finding carries its evidence and date:

| Label | Meaning |
|---|---|
| **Live** | A request we sent ourselves: from a signed-in browser page with `fetch`, or from a standalone script with a personal access token (PAT). Both are named. |
| **Browser** | Seen in a browser capture taken around a controlled action. The capture is not kept. |
| **Source** | Present in Confluence's web JavaScript or HTML. Not proof of use. |

This is an anonymized record of one Confluence Data Center 9.2.10 instance and one account, not a
capability matrix. Administrators set indexing language, rate limits, plugins and permissions; rerun
the probes in [`research/`](../research/README.md) before relying on a finding elsewhere, and keep a
missing permission, a bad request, an unavailable route and an empty result apart. Writes were made
only to synthetic `Connector Lab` pages in the personal space, with the user's authorization for each
run, and nothing was purged. No tokens, cookies, page content, names or real ids are kept here.

## 1. Summary

- Every listing caps a page at 500 (50 with `body.storage`) but echoes the requested `limit`, and
  `content/search`'s `next` link then skips results: page on the returned `start + size` (§5.2).
- Search splits terms at hyphens and dots but keeps underscored identifiers whole; quoting a title
  with punctuation misses, and replacing the punctuation with spaces finds it (§4.2, §4.3).
- CQL takes dates only as zone-less `yyyy/MM/dd [HH:mm]` in an unstated zone (§4.4).
- No route returns plain text, and `view` loses Jira keys, PlantUML source and the HTML macro's
  identity, which only `storage` keeps (§5.1).
- A content update with `ancestors: []` is accepted and ignored; the private `movepage.action` moves to
  the root, reorders, reparents and moves across spaces, without a page version (§6.3).
- Trash loses the hierarchy: Confluence always restores at the space root, and CQL never sees trashed
  pages (§6.4).

## 2. Instance and credentials

### 2.1 Instance

Live (browser), 2026-10-19:

- Version 9.2.10, build 9111 (`ajs-version-number`). AUI 9.12.11 is a frontend asset version.
- Collaborative editing is off (`collaborative-content=false`, `shared-drafts=false`, empty Synchrony
  settings), so drafts are the legacy per-user editor drafts.
- About 100 macros from standard plugins, plus multi-excerpt, PlantUML, numbered headings, a Git
  plugin, tree view, image map and the **HTML macro**, which is enabled. No draw.io or Gliffy.

Live REST, 2026-10-20: personal-space keys can contain dots and `@` after `~`; preserve account-name
punctuation when configuring or resolving personal spaces.

### 2.2 Credentials

- **PAT, Live (script), 2026-10-07 and 2026-10-19:** a bearer PAT works for every read and write the
  connector makes, including the private `movepage.action` with `X-Atlassian-Token: no-check` and no
  `atl_token`. A REST `POST` with a bearer needs no `X-Atlassian-Token` header.
- **Revoked PAT, Live (script), 2026-10-30:** the current-user check answers 401, which the connector
  reports as `AuthenticationRequired` without touching the stored credential. Expiry is untested
  ([roadmap](roadmap.md#live-verification)).
- **Anonymous, Live, 2026-10-07:** 401 on current user, both searches and spaces.
- **Single sign-on, Browser, 2026-10-07:** sign-in goes `/login.action` →
  `/plugins/servlet/external-login/{id}` → Microsoft SAML → `POST /plugins/servlet/samlconsumer` →
  session cookie. It yields no token a local process can use.
- **Form login, Browser and Live (probe), 2026-10-07:** the undocumented
  `POST /rest/tsv/1.0/authenticate` takes JSON `username,password,rememberMe,targetUrl,captchaId`;
  failure is 401 with `message,captchaRequired`, success 200 with `next`. A copied cookie jar worked
  immediately in a fresh client; lifetime is unknown. Used only by the research probe, never by the
  connector.
- Native OAuth needs an administrator-registered client and was not tested.

## 3. Transport

Live, 2026-10-19:

- The server speaks HTTP/1.1 only; connections are reused.
- No 429, `Retry-After` or `X-RateLimit-*` header appeared in hundreds of mostly sequential requests,
  six and ten in parallel (browser), or 50 reads at ten in parallel (PAT). Data Center adds those
  headers only when an administrator enables
  [rate limiting](https://confluence.atlassian.com/doc/improving-instance-stability-with-rate-limiting-992679004.html).
- Server time dominates: about 270 ms to first byte, median about 280 ms per page read. Six parallel
  reads took 0.85 s in total; 50 sequential took 15.4 s.
- One `GET` expanding `body.view`, `body.storage`, `version`, `history`, `children.attachment` and
  `children.comment.body.view` took 1.0–3.4 s; the same data as three parallel requests (bodies,
  `child/attachment`, `child/comment?depth=all`) took 0.4–0.75 s. The embedded expansions are also
  incomplete: `children.*` stops at 25, and comments omit replies.

## 4. Search

### 4.1 Endpoints and archived spaces

Live (browser and PAT), 2026-10-19:

- `GET /rest/api/space` documents a `status` parameter without listing its values. `current` and
  `archived` are the status names spaces carry, so the connector asks for each.
- Both `content/search` and `rest/api/search` leave archived spaces out, even for an explicit
  `space = <archived key>`. Only `rest/api/search` with `includeArchivedSpaces=true` adds them (about
  8% more pages here); `content/search` ignores the parameter. Personal spaces are included.
- `space.status` is not a CQL field. Invalid CQL returns 400 with a log referral.
- `page`, `blogpost`, `comment`, `attachment`, `space` and `user` results are all searchable. A
  comment hit is the comment, not its page. Attachment hits carry an excerpt of the indexed file
  content and `resultParentContainer`.
- Pages edited within the hour are found; most searches take 0.3–1 s.
- `rest/api/search` row `title` and `excerpt` are display snippets (PAT, 2026-10-29): matched terms
  sit between `@@@hl@@@` and `@@@endhl@@@`, and characters are HTML-escaped (`&amp;`, `&#39;`). The
  nested `content.title` is plain. Rows without nested content (users) carry only the snippet title.
- The current/archived space listing matches the UI space directory (PAT and browser, 2026-10-30):
  the same totals for each status and type, and the same set of archived keys.

### 4.2 Terms and punctuation

Live (browser), 2026-10-19 and 2026-10-20:

- `siteSearch ~ "a b"` is an OR of the words (its total exceeds either word alone); `text ~ "a b"` is
  an AND.
- A hyphen splits a term: `ci-cd` matched 2,160 pages and `"ci-cd"` 482 (2026-10-20). A dotted pair of
  prose words, `a.b`, matched nothing, while the phrase `"a b"` found the page.
- An underscore joins: `a_b` is one token, matched exactly or by `a_*`. A code identifier shaped
  `x_y.z` matched only when searched whole; every search for its parts missed.
- Case-insensitive, accent-folding, English stemming (`deploy`, `deployed`, `deployments` match each
  other; `configure` and `configuration` do not), no Portuguese plurals. The administrator's
  [indexing language](https://confluence.atlassian.com/doc/configuring-indexing-language-150130.html)
  decides this and is not visible to users.
- `word*`, `de*oy` and `?` work; a leading `*` returns almost nothing. Escaped wildcards stay literal.

### 4.3 Titles

Live (browser), 2026-10-19, 31 recent pages, one per space:

- Quoted `siteSearch` and `title ~` each missed 6 titles, always ones containing `-/:()",`.
- For 8 such titles: written as is (only `\` and `"` escaped) found 3 and 2; escaping CQL reserved
  characters with `\\`, as the [CQL documentation](https://developer.atlassian.com/cloud/confluence/cql-functions/)
  suggests, found 6 and 6; replacing punctuation with spaces found 8 and 7, at rank 1 except one at
  rank 7. A [developer forum report](https://community.developer.atlassian.com/t/cql-title-search-with-special-characters/13601)
  describes the same.
- `title = "exact"` matches unless the title contains `"`.
- The private quick-navigation route behind the header search, `GET /rest/quicknav/1/search`
  (`query`, `limit`, `type`), ranked 30 of 31 titles first and returned all 31. It accepts no filters,
  and `type=user` returns 500.
- Quick-navigation contract (PAT and browser, 2026-10-30): `contentNameMatches` is a list of groups,
  each a list of rows with `id`, `name`, `href`, `className`, `spaceKey` and `spaceName`. Without
  `type` the groups are pages (`content-type-page`), attachments (`content-type-attachment-*`), spaces
  and one `search-for` row with no `id`; `type=page` leaves the page group and `search-for`. The UI's
  `src=next.ui.search` parameter makes every query return no matches. It finds pages in personal
  spaces (a fixture page at rank 1) but none in archived spaces (0 of 8 recent pages from 4 archived
  spaces); archived titles come only from CQL with `includeArchivedSpaces=true`.

### 4.4 Dates and time zones

Live (browser), 2026-10-19:

- CQL accepts `yyyy/MM/dd`, `yyyy-MM-dd`, `yyyy/MM/dd HH:mm` and `yyyy-MM-dd HH:mm`. Seconds, a `T`,
  `Z`, numeric offsets and zone names return 400 "Could not parse cql". The documentation does not say
  which zone a literal is read in.
- A binary search of `lastmodified >=` minute cutoffs on one page matched its REST modification
  instant at the same minute. The account's profile zone and the measured offset were both `+00:00`,
  so this cannot tell UTC, profile zone and server zone apart
  ([roadmap](roadmap.md#live-verification)). REST timestamps are UTC `Z` instants, and
  `/rest/api/user/current` exposes no zone.
- A date-only upper bound includes the whole day (2026-10-20). `now("-Nm")` works at minute precision.

Live REST, 2026-10-20: search modification times and REST `version.when` differed by one millisecond
on a stable page. Calibration must compare REST reference instants before/after the measurement;
search only selects the sample. Corrected calibration and date-only/offset-timestamp filters passed
with the saved effective offset zero. Repeated daylight-saving hours remain untested.

Live (PAT and browser), 2026-10-30: CQL follows the server zone, not the user's profile. With the
profile at site default (UTC+00:00) and then at `America/Sao_Paulo` (UTC−03:00), calibration measured
an offset of zero both times. The profile settings form lists "Site Default" with the site zone's own
value, so saving the form stores that zone explicitly.

### 4.5 Users

Live (browser), 2026-10-19:

- `rest/api/search` with `type = user AND user.fullname ~ "name"` finds current users;
  `rest/api/search/user` returns 404.
- Former users: 24 of 26 authors of the oldest pages were missing from user search, show their
  username as display name, and are not matched by `creator.fullname ~`. `GET /rest/api/user?username=`
  still returns them, and `creator = "username"` still finds their pages.
- `creator` and `contributor` take a username, not a display name.

### 4.6 Ordering and latest-version author fields

Documentation review, 2026-10-21:

- Atlassian's [Data Center CQL guide](https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/)
  documents `ORDER BY` and date ordering generally. It does not document this instance's observed
  `siteSearch` field or state whether that private search clause accepts a sort suffix.
- The [REST expansion guide](https://developer.atlassian.com/server/confluence/expansions-in-the-rest-api/)
  documents expanding `version` and shows its `by` author. The connector's search request already
  expands `content.version` for `when`; the parser now also reads `by` from that same expansion.

Live read-only check, 2026-10-21:

- On the configured Data Center instance, a text query using `siteSearch` succeeded with each of
  `ORDER BY lastmodified DESC`, `ORDER BY lastmodified ASC` and `ORDER BY created DESC`; each returned
  three hits. The returned modification timestamps followed the requested order for both modified
  sorts. A queryless `contributor IN ('<configured-user>')` page listing also succeeded, defaulted to
  `type IN (page)` and `ORDER BY lastmodified DESC`, and returned three hits in descending modification
  order.
- All sampled hits included `version.by`; the mapped `last_modified_by` included both username and
  display name in the initial text and queryless samples. The API still permits a missing author, which
  maps to null. No missing-author result occurred in these samples.
- The two initial compact responses were 2,254 and 2,612 JSON characters, below the 40,000-character
  default. This did not stress the size guard. No pre-change latency or payload comparison was made;
  the search request already expanded `content.version`, so the change adds no request or expansion.
- This verifies behavior on one configured instance only. The search hits do not expose creation time,
  so the `created DESC` result order was accepted by Confluence but not independently checked against
  per-hit creation timestamps. Support across other Data Center versions remains unknown.

## 5. Reading pages

### 5.1 Bodies and macros

Live (browser), 2026-10-19:

- `storage`, `view`, `export_view`, `styled_view` and `editor` are readable. `view` keeps
  `data-macro-name` wrappers and attachment identity (`data-linked-resource-id`, `-container-id`,
  `-default-alias`, media type); `export_view` drops both.
- No route returns plain text. `POST /rest/api/contentbody/convert/{to}` returns 500 for `wiki` and
  `plain` and 404 for `text`, `atlas_doc_format` and `anonymous_export_view`.

| Macro | In `view` | Kept only in `storage` |
|---|---|---|
| `include`, `excerpt-include` | Included text, resolved inline | The `ri:page` reference |
| `expand` | Full body, hidden by a class | |
| `code` | Text and a `language` parameter | |
| `plantuml` | A client-side script, no image | The PlantUML source |
| `jira` | An "Unable to render" warning | `key`, `server`, `serverId`, `columns` |
| `html` | Its raw output, scripts included, with **no wrapper** | That it is the HTML macro |

Included pages (PAT, 2026-10-30): the host's `view` keeps the included page's macro wrappers, such as
`status` and `expand`, inside the `include` wrapper, but an included HTML macro's output stays
wrapperless. Only the included page's own `storage` names it. On its own page, the HTML macro was
added through the editor's macro browser; the "Insert markup" dialog's `{html}` became an
`unmigrated-wiki-markup` macro instead.

Inline-comment anchors appear as `ac:inline-comment-marker` in storage and in the rendered output.
Of 200 pages edited since 2025, 19 use `ac:layout` columns; of 105 with headings, 16 have headings
inside layout cells and 9 inside macro bodies.

Live REST, 2026-10-20: `view-file` requires an attachment resource in its `name` parameter:
`<ri:attachment ri:filename="synthetic.txt" />`. Filename text instead returned HTTP 500 because
Confluence attempted to cast a string to `AttachmentResourceIdentifier`; the resource form succeeded.

### 5.2 Paging

Live (browser and PAT), 2026-10-19 and 2026-10-20:

- `content/search`, `rest/api/search` and `rest/api/content` return at most 500 per page, and
  `content/search` with `body.storage` at most 50. Every one reports the **requested** `limit`, not
  the applied one, and `content/search`'s `_links.next` then advances by the requested limit
  (`start=1000` after 500 results), skipping 500. `rest/api/search` gives no `next` link.
- `start=10000` works on both searches.
- `child/page` returned all 205 children at `limit=500`, but only 200 with `expand=position`.
  `children.page` as an expansion reports 25. `descendant/page` returns 500 for every page and limit
  tried; CQL `ancestor = {id}` works.
- `child/page` and `space/{key}/content/page?depth=root` send `_links.next` only while more results
  exist, with `start` equal to `start + size`; a short page without one was final in every case
  (2026-10-20). All 115 visible global spaces returned their root listing in one request at 500, and
  `/rest/api/space` returned all 115 at 500.

Live REST, 2026-10-20: newly created pages and ancestors can report `position: -1` with extension
position `"none"`. These denote unordered placement, not invalid counts or versions.

### 5.3 Labels

Live (browser), 2026-10-20: the `metadata.labels` expansion is a paged envelope (`results`, `start`,
`limit`, `size`, `_links`) of 200. `GET /rest/api/content/{id}/label` caps `limit` at 200, reports the
applied limit (unlike the searches) and pages with `_links.next`. The largest label set among 100
sampled pages was 6.

### 5.4 Attachments, comments and versions

Live (browser and PAT), 2026-10-19:

- `child/attachment` returned all 38 attachments at `limit=500`. Downloads use
  `/download/attachments/{container}/{filename}?version=…&api=v2`: 200, no redirect, the attachment's
  own media type and size, for a browser session and a PAT. `/rest/api/content/{id}/download` is 404.
- `child/comment` returns top-level comments only; `depth=all` adds replies, which carry comment
  ancestors. `extensions.location` is `footer` or `inline`; inline comments carry `originalSelection`
  and `markerRef`; resolution is `open`, `resolved` or `dangling`.
- `/rest/api/content/{id}/version` is 404. The experimental `/rest/experimental/content/{id}/version`
  lists versions (200 per page, `next` link), and `?status=historical&version=N` reads old bodies,
  including version 1 of a page at version 738.

## 6. Writes

Live (browser), 2026-10-19 unless dated otherwise, on synthetic personal-space pages; the PAT
repeated create, update, label, parent change, trash and the move action.

### 6.1 Pages and versions

- Update needs `version.number` = current + 1; a stale or skipped number is 409 "Version must be
  incremented". Of two parallel writers on one version, one gets 200 and the other 409.
- `minorEdit: true` was sent but later reported as `false`. It still suppresses the edit email (PAT
  and editor, 2026-10-30): unchecking the editor's "Notify watchers" sends exactly
  `version.minorEdit: true` to `PUT /rest/api/content/{id}`. On four watched synthetic pages, edits
  with it sent no email; edits without it did, about 9 minutes later (batched).
- An update with the stored title and a byte-identical body creates no version (PAT, 2026-10-30): the
  response and later reads keep the current version. `view` differs between any two reads of an
  expand macro, whose element IDs are random, so compare `storage`, not `view`.
- Write bounds (PAT, 2026-10-30): a 255-character title saves and 256 is 400. Bodies of 2,000,000 and
  5,000,000 characters both save. A 255-character label saves; 256 answers 500 and is not stored. A
  new-page draft does not block a current page with the same title; a current page does (400).
- A rename keeps the ID; the `/x/` short link follows the page, while the old `/display/{space}/{title}`
  URL is 404.
- A second attachment with the same filename is 400; a new version goes to
  `POST …/child/attachment/{attachmentId}/data`. Uploads do not change the page version.
- Creating an inline comment through REST (`extensions.location: inline`) is 400 "Could not create
  content with type comment"; the UI uses the inline-comments plugin.

### 6.2 Drafts

- `POST` with `status: draft` creates a new-page draft, readable only with `?status=draft` and absent
  from search and child listings. `GET /rest/api/content?type=page&status=draft` lists the user's
  drafts, across spaces or per `spaceKey`; CQL with `contentStatuses: ["draft"]` does not.
- A draft cannot be updated without publishing: `PUT …?status=draft` with `status: draft` is 400.
- Publishing is `PUT /rest/api/content/{draftId}` with no query, `status: current`, version 1, `space`
  and `ancestors`: 200, same ID. The same body to `…?status=draft` is 400 without `space` and 404 with
  it.
- `DELETE /rest/api/content/{id}?status=draft` is permanent: the draft is then in neither the draft
  list, the trash nor the Drafts page.
- Unpublished edits of a published page go through the private `POST /rest/tinymce/1/drafts`; they are
  listed on the Drafts page but cannot be read through REST. Not used (§7).

### 6.3 Moves

- `PUT /rest/api/content/{id}/move/{position}/{target}` is 404 on 9.2.10, and the copy routes
  `…/copy` and `…/pagehierarchy/copy` are 404 too.
- A content update with new `ancestors` changes the parent and **creates a page version**; children,
  attachments and comments follow.
- **`ancestors: []` is ignored** (2026-10-20): 200, but the page keeps its parent, does not appear among
  the root pages and gets no new version.
- The UI's move dialog posts `POST /pages/movepage.action` (Source and Live) with `pageId`, the target
  `spaceKey`, `mode` (`LEGACY` or `ASYNC`) and one of: `position` `above`, `below` or `append` with
  `targetId`; `targetTitle` with `append`; or `position=topLevel`. It answers JSON with
  `validationErrors`, `actionErrors` and `errorMessage` on failure, and `ASYNC` adds `longTaskId`.
  None of its moves creates a page version.
  - `above` and `below` reorder siblings (2026-10-19).
  - `topLevel` moves the page to the space root, first among the root pages (2026-10-20).
  - `above` with a target under another parent reparents and positions in one call; `append` with a
    parent as target makes the page its last child (2026-10-20).
  - Across spaces with `ASYNC`, `GET /rest/api/longtask/{id}` reported `successful: true`, "Page move
    complete.", after about 0.3 s. ID, version, children, attachments, comments and labels were kept,
    and the move back worked the same way.

Live REST, 2026-10-20: a separately authorized cross-space round trip preserved the page version;
independent metadata confirmed both destinations, and child listing confirmed immediate cleanup of
the temporary destination. No destination identifiers or private configuration were retained.

Live (PAT), 2026-10-30: `move_page` moved a synthetic page with a child and a label under a parent
page in another global space, then back to the personal space root. Each long task finished in about
1.1 s; the ID, version 1, child and label were unchanged at both destinations.

### 6.4 Trash and restore

- `DELETE /rest/api/content/{id}` is 204; the page is then 404 unless read with `?status=trashed`. A
  second `DELETE` is 404: it does not purge. **The trashed page's child becomes a top-level page**,
  not a child of the grandparent.
- Restore is `PUT /rest/api/content/{id}?status=trashed` with `status: current` and the next version
  number: 200, version unchanged. **The page returns at the space root**, and so does a child whose
  parent was trashed and restored. The UI's own *Restore* (`/pages/restoretrashitem.action`) does the
  same after a REST delete and after the UI's *Delete Page*.
- `GET /rest/api/content?type=page&status=trashed` lists the trash per `spaceKey` or across every
  visible space, including spaces the user does not administer; `type=blogpost` works,
  `type=attachment` is 500. Rows show no ancestors and only the last edit, not who deleted or when.
- CQL never returns trashed pages, even with `contentStatuses: ["trashed"]`, apart from a few seconds
  of index lag right after the trash (2026-10-20). The same title lookup with `status=trashed`
  (`?type=page&spaceKey=…&title=…&status=trashed`) finds a trashed page; without `status` it does not
  (2026-10-20).
- A content property (`POST /rest/api/content/{id}/property`) adds no page version. While the page is
  trashed, reading it is 404 (with or without `?status=trashed`) and listing or expanding properties is
  500; after restore it is back unchanged. A property is therefore a way to carry the old placement
  across the trash.
- `expand=operations` on a page lists the user's operations including restrictions: `delete` is there
  in the personal space and missing in a space where the user cannot delete. On a space it returns an
  empty list. `GET /rest/api/space/{key}?expand=permissions` lists permissions only in a space the user
  administers and is 500 elsewhere. `/rest/api/content/{id}/permission/check` is 404.

Live REST, 2026-10-20: a successful restore response omitted space/version expansions. A fresh current
read returned them, with version unchanged; placement must use fresh metadata after confirmed restore.

### 6.5 Rich-text input

25 snippets of typical AI-generated HTML and storage tags, checked with `contentbody/convert` and
saved where safe:

| Input | Result |
|---|---|
| Headings, inline styles, nested lists, `blockquote`, tables with `thead`, `colspan` and alignment, task lists, entities | Stored unchanged (entities as characters) |
| `<br>`, `<hr>` (HTML5 void) or an unclosed tag | Save is 400 "Error parsing xhtml" with row and column, while `convert` returns 200 with a `fatal-render-error` block |
| `<pre><code class="language-…">`, plain `<pre>` | Stored, but rendered as bare monospace: no panel, highlighting or language |
| Code, panel, expand, status, table-of-contents, emoticon, mention and page-link macros | Stored; every macro gains `ac:schema-version` and a stored `ac:macro-id` that `view` does not show |
| `<details>`, `<summary>`, `<mark>`, `<kbd>` | Tags stripped, text kept |
| `<script>`, `<style>`, `onclick`, `javascript:` links, the HTML macro | Passed through `convert` unchanged; not saved |

SVG images (PAT and browser, 2026-10-30, Data Center 9.2.10, one synthetic page): an SVG attachment
uploaded as `image/svg+xml` renders inline through `<ac:image><ri:attachment …/></ac:image>` and
through `<ac:image><ri:url …/></ac:image>` on its download URL; `view` emits a plain `<img>`, and the
editor keeps both as images. The `view-file` macro, which drag-and-drop inserts, shows a placeholder
icon (`data-has-thumbnail="false"`) because no SVG thumbnail exists. An inline `<svg>` element is
stripped on save, leaving an empty paragraph.

## 7. Private routes seen but not used

Browser and Source, 2026-10-07 to 2026-10-19. Contracts are kept only where a request was sent.

| Route | What it is |
|---|---|
| `/rest/spacedirectory/1/search` | Space directory search (`query`, `status`, `type`, paging); answers `spaces,totalSize` |
| `/rest/inlinecomments/1.0/comments?containerId=` | Inline comments with author and permission fields |
| `/plugins/pagetree/naturalchildren.action` | Every child page as HTML |
| `/plugins/macrobrowser/browse-macros.action` | The installed macro list |
| `POST /rest/tinymce/1/drafts`, `DELETE …/drafts/discard` | Unpublished edits of a published page (200; not readable through REST) |
| `/exportword?pageId=` | Word export: an HTML document inside a MIME wrapper |
| `/spaces/flyingpdf/pdfpageexport.action?pageId=` | PDF export: 403 for this account |
| `/plugins/viewstorage/viewpagestorage.action?pageId=` | Storage as `text/plain`, the same as `body.storage` |
| `/rest/prototype/1/search/user.json` | Prototype user search |
| `/pages/reorderpages.action`, tree-copy and tree-delete actions | Source only |
