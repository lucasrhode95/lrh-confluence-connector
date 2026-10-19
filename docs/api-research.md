# Confluence API research

Current deployment-specific findings that Atlassian does not document, documents ambiguously, or that
settle an integration question. Public API reference material stays at its official source.
Consolidated 2026-10-18; earlier evidence retains its original date.

## How to read this record

**Live** means a user-run standalone probe. **Browser** means a request observed in a browser capture.
**Source** means a route or behavior found in saved UI code/HTML, not a verified API call.
**Documentation** means a vendor description, not a successful deployment test.

Evidence describes one user, credential, query and instance. Neither an empty response nor a source
fragment proves a universal restriction or capability. Browser sessions and PATs are different
credentials. Captures are temporary private inputs outside the repository; only useful route shapes,
contracts and summarized findings belong here. No content mutations have been verified by these records.

## Authentication and deployment

**2026-10-07 — Browser/source:** saved HTML identifies Confluence Data Center **9.2.10**. AUI 9.12.11
is a frontend asset version, not the server version.

| Method | Evidence and observed result | Limit |
|---|---|---|
| PAT bearer | Live: HTTP 200 for current user, content search, UI search, spaces and one content ID | Token type relies on the user's PAT-creation report; bearer transport alone cannot distinguish PAT from OAuth |
| Form session | Browser and two live runs: login and the same API reads succeed; immediate cookie replay in a fresh HTTP client succeeds | In-memory replay only; no persistence, expiry or reboot claim |
| Anonymous | Live: HTTP 401 for current user, both searches and spaces | Tested protected reads only |
| Basic | Probe available; no successful result recorded | Form success does not prove Basic acceptance |
| Native OAuth | Documentation and token-accepting probe mode only | No administrator registration, token grant or native-provider API test |
| Microsoft SAML SSO | Browser: sign-in returns to the Wiki SAML consumer and authenticated content | Does not provide an OAuth token to the local connector |

### Form session contract

**2026-10-07 — Browser/live:** `POST /rest/tsv/1.0/authenticate` accepts JSON keys
`username,password,rememberMe,targetUrl,captchaId`. Query key `os_authType` was observed; shared source
uses `os_authType=none`. A failed browser login returned 401 with `message,captchaRequired`; a
successful one returned 200 with `next`. This is an undocumented browser endpoint, isolated to the
interactive probe, not the connector's authentication backend.

Two user-run `form --remember-me` probes reported two Secure/HttpOnly cookies, one with an expiry.
A copied cookie jar immediately passed current-user and CQL reads in a fresh client. No credentials
or cookies were saved; this proves neither durable reuse nor a particular lifetime.

**Browser:** `GET /rest/authconfig/1.0/login-options` returned
`results,size,start,limit,isLastPage`, with `LOGIN_FORM` and `IDP` options. This establishes a configured
identity-provider choice, not OIDC. The SSO sequence was `/login.action` →
`/plugins/servlet/external-login/{id}` → Microsoft SAML →
`POST /plugins/servlet/samlconsumer` (`SAMLResponse,RelayState`) → Wiki session.
Same-password behavior does not establish the underlying directory technology.

CAPTCHA failure was observed. TOTP/recovery/enrollment code was present in UI resources but not
verified as enabled or exercised; no enrollment or permission elevation was attempted.

### Documented alternatives with unresolved applicability

**2026-10-07 — Documentation comparison:** the native OAuth provider guide's bearer-content example
and earlier versioned REST OAuth notices appeared inconsistent. Without a registered-client test,
neither native OAuth support nor impossibility was established. This is not a selected implementation
path; administrator assistance is unavailable for the intended deployment.

Service-account availability was not established for the tested version. A hosted Teamwork Graph
bridge was discussed but not configured or tested; it requires an administrator/cloud setup outside
the agreed local product. Neither option is evidence of a usable credential here.

References: [PAT guide](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html),
[native OAuth provider](https://developer.atlassian.com/server/confluence/confluence-oauth2-provider-api/).
Credential expiry, replacement, local removal versus server revocation, account binding and encrypted
cross-process persistence have not been verified.

## Standalone reads

**2026-10-07 — Live, PAT and form-session probes:**

| Operation | Route | Result and limit |
|---|---|---|
| Identity | `GET /rest/api/user/current` | 200 and an account-identifier signal; identity values not retained |
| Content search | `GET /rest/api/content/search` | 200, one result with `limit=1` |
| UI search | `GET /rest/api/search` | 200, one result with `limit=1` |
| Spaces | `GET /rest/api/space` | 200, one result with `limit=1` |
| Content | `GET /rest/api/content/{id}` | One selected result returns JSON |

The content probe supplied `cql=type = page AND siteSearch ~ <escaped text>`, `limit=1`,
`expand=space,version`. UI search added `start=0,src=next.ui.search,expand=space.icon` and
`includeArchivedSpaces=false`. Those are probe parameters, not proof of filter semantics or archived
coverage. Current-user success is standalone evidence, not a claim that it appeared in the captures.

The sample proves credential access to these operations, not search recall, exhaustive coverage,
paging, attachment retrieval, macros, permission completeness or writes.

## Search and content browser contracts

**2026-10-07 — Browser:** these useful contracts were observed independently of the PAT probe.

| GET route | Observed query keys | Observed response shape |
|---|---|---|
| `/rest/api/content/search` | `cql,cqlcontext,expand,_` | `_links,cqlQuery,limit,results,searchDuration,size,start,totalSize` |
| `/rest/api/search` | `cql,excerpt,expand,includeArchivedSpaces,limit,src,start` | Same search envelope |
| `/rest/experimental/search` | `cql,expand,limit,_` | Same search envelope |
| `/rest/quicknav/1/search` | `limit,query,src,type` | `contentNameMatches,query,queryTokens,totalSize` |
| `/rest/spacedirectory/1/search` | `filterUnauthorizedSpaces,pageSize,query,startIndex,status,type,_` | `spaces,totalSize` |
| `/rest/inlinecomments/1.0/comments` | `containerId,_` | No object envelope asserted |
| `/plugins/pagetree/naturalchildren.action` | `pageId,ancestors,sort,startDepth,expandCurrent` and other tree options | HTML/UI response |
| `/plugins/macrobrowser/browse-macros.action` | `detailed` | `categories,editTitle,insertTitle,macros,title` |
| `/plugins/editor-loader/editor.action` | Editor options including `pageId,parentPageId,spaceKey,atl_token` | Editor HTML/resources; no token values retained |

Browser attachment downloads used `/download/attachments/{id}/{filename}`. Standalone PAT attachment
access, redirects and filename handling remain unverified. Plugin reads are not automatically covered
by successful PAT calls to standard REST routes.

### Source leads for required capabilities

**2026-10-07 — Source only unless stated otherwise:**

- Attachment listing: `/rest/api/content/{id}/child/attachment`; upload code also references
  `/plugins/drag-and-drop/upload.action`. No independent attachment/upload test is recorded.
- Draft/editor work: `/rest/create-dialog/1.0/blueprints/web-items`,
  `/rest/create-dialog/1.0/storage/quick-create`, `/rest/tinymce/1/macro/preview` and
  `/rest/tinymce/1/drafts/discard`. Mutation methods/outcomes are unverified.
- Moves/tree deletion: `/pages/movepage.action`, `/pages/reorderpages.action`,
  `/plugins/tree-copy/preparing-copying.action`, `/plugins/tree-delete/preparing-delete.action`.
  These are discovery leads, not chosen connector APIs or authorization to execute them.
- Macro/plugin code includes multi-excerpt and numbered-heading features. Source presence does not
  establish that a plugin is installed, enabled or callable with the integration credential.

**2026-10-18 — Source:** an additional home-page resource export contains search, editor/draft,
content-child/descendant and history route references, including `/pages/resumedraft.action` and
`/rest/api/content/{id}/history`. This was static inspection only. It establishes no new HTTP status,
write behavior or successful standalone endpoint. No archive/member inventory is retained.

## Read-only browser-session probes

**2026-10-19 — Live, browser session:** user-authorized read-only `GET` requests sent from a signed-in
page with `fetch`, following runbook sessions 0–2b. No HAR was kept and nothing was written. One
instance, one account; counts are rounded indicators, not limits that apply everywhere. PAT behavior on
the same routes was not retested.

### Instance profile

Version 9.2.10 (`ajs-version-number`), build 9111. The macro browser lists about 100 macros from
standard Atlassian plugins plus multi-excerpt, PlantUML, numbered headings, a Git plugin, tree view,
image map and the HTML macro. No diagram editor such as draw.io or Gliffy appeared. The personal
access token page loads for this account. Whether collaborative editing is enabled was not determined
without opening an editor.

### Search

| Question | Result |
|---|---|
| Page caps | `content/search` returns at most 500 without body expansion, 50 with `body.storage`. `rest/api/search` returns at most 500. `rest/api/content?type=page` returns at most 500 and took about 9 s |
| Echoed limit | When the cap applies, both report the requested `limit`, not the applied one. `content/search` `_links.next` then advances by the requested limit (`start=1000` after 500 results), skipping results. Page on `start + size` |
| Deep paging | `start=10000` works on both. `rest/api/search` gives no `next` link |
| Known title (31 recent pages, one per space) | Quick navigation `/rest/quicknav/1/search` ranked 30/31 first and 31/31 in its results. Quoted `siteSearch` and `title ~` each missed 6, always titles with `-/:()",`. Replacing punctuation with spaces found 9/9 with `siteSearch` and 8/9 with `title ~`. `title = "exact"` matched unless the title contained `"` |
| Body phrase | Quoted phrases found every page whose phrase was really contiguous, mostly at rank 1. All 5 misses were probe artefacts: phrases joined across element boundaries |
| Unquoted words | `siteSearch ~ "a b"` is an OR match (total higher than either word alone); `text ~ "a b"` behaves as AND. Unquoted `siteSearch` put the target first for 15/31 pages and in the top 10 for 24/31 |
| Normalization | Case-insensitive. Accents are folded (Portuguese words with and without diacritics return identical totals). English stemming (`deploy`, `deployed`, `deployments` identical; `configure` ≠ `configuration`). No Portuguese plural stemming. `word*`, `de*oy` and `?` wildcards work; a leading `*` returned almost nothing |
| Identifiers | Hyphenated `a-b` is split; quoting it equals the phrase `"a b"`. Dotted `a.b` returned nothing, while the phrase `"a b"` found it. Underscore `a_b` is one token, matched only exactly or by `a_*` |
| Filters | `space`, `creator`, `contributor` (username, not display name), `label`, `parent`, `ancestor`, `created`, `lastmodified` all matched as expected. `space.status` is not a CQL field. Invalid CQL returns 400 with a log referral |
| Archived spaces | Excluded by default from both endpoints, including explicit `space = <archived key>`. Only `rest/api/search` with `includeArchivedSpaces=true` adds them (about 8% more pages); `content/search` ignores that parameter. Personal spaces are included by default |
| Types | `page`, `blogpost`, `comment`, `attachment`, `space` and `user` are searchable. Comment text finds the comment, not its page. Attachment results carry an excerpt (indexed content), `resultParentContainer` and media type |
| Latency | Most searches took 0.3–1 s, including pages edited within the last hour |

### Bodies and macros

`storage`, `view`, `export_view`, `styled_view` and `editor` are all readable. None contained base64
except one CSS data URI in `styled_view`. `view` keeps `data-macro-name` wrappers and attachment
identity (`data-linked-resource-id`, `-container-id`, `-default-alias`, MIME type). `export_view` is
rendered, but it drops the macro wrappers and attachment IDs. Large pages took 3–6 s per
representation.

| Macro | `view` result | Storage source |
|---|---|---|
| `include`, `excerpt-include` | Included text resolved inline | `ri:page` reference |
| `expand` | Full body present with a hidden class | Rich-text body |
| `code` | Code text plus a `language` parameter | Plain-text body |
| `plantuml` | Client-side script; no server-rendered image | PlantUML source text |
| `view-file` | Embedded-file link with attachment ID and filename | `ri:attachment` |
| `jira` | "Unable to render" warning, also in the UI page HTML | `key`, `server`, `serverId`, `columns` |
| `html` | No `data-macro-name` wrapper found | Unresolved |

Inline-comment anchors appear in storage (`ac:inline-comment-marker`) and in rendered output.

### Trees, attachments, comments, versions

- `child/page` returned all 205 children with `limit=500`; one call with `expand=position` stopped at
  200. An embedded `children.page` expansion reports only 25.
- `descendant/page` returned HTTP 500 for every page and limit tried. CQL `ancestor = {id}` works.
  `/plugins/pagetree/naturalchildren.action` returns every child as HTML.
- `child/attachment` returned all 38 with `limit=500`, including `fileSize`, `mediaType` and the
  download link `/download/attachments/{container}/{filename}?version=…&api=v2`. A browser download
  returned 200 with no redirect and `inline` disposition. `/rest/api/content/{id}/download` returned 404.
- `child/comment` returns top-level comments only. `depth=all` adds replies, which have comment
  ancestors. `extensions.location` is `footer` or `inline`. Inline comments carry `originalSelection`
  and `markerRef`. Resolution status takes the values `open`, `resolved` and `dangling`.
  `/rest/inlinecomments/1.0/comments` adds author and permission fields.
- `/rest/api/content/{id}/version` returned 404; `/rest/experimental/content/{id}/version` lists
  versions (200 per page, `next` link). `?status=historical&version=N` reads old bodies, including
  version 1 of a page at version 738.

### Follow-up reads

**2026-10-19 — Live, browser session:** the edit page reports `collaborative-content=false`,
`shared-drafts=false`, empty Synchrony settings and `draft-save-interval=30000`. Collaborative editing is
off; drafts use the legacy per-user editor drafts. The HTML macro is enabled: its raw HTML, including
scripts, is injected into `view` verbatim with no `data-macro-name` wrapper, so only storage identifies
it. Search by a deactivated contributor was not tested.

## Authorized writes in a personal space

**2026-10-19 — Live, browser session:** user-authorized writes to synthetic `Connector Lab` pages in the
account's personal space, using documented REST unless marked undocumented. Requests were sent from
the signed-in page with `X-Atlassian-Token: no-check`. Nothing was purged and no other content was
touched.

| Operation | Request | Result |
|---|---|---|
| Create page | `POST /rest/api/content` with `space`, `ancestors`, storage body | 200, version 1 |
| Label | `POST /rest/api/content/{id}/label` | 200 |
| Upload attachment | `POST …/child/attachment` (multipart) | 200; page version unchanged. Same filename again: 400. New attachment version: `POST …/child/attachment/{attachmentId}/data` |
| Update page | `PUT /rest/api/content/{id}` with `version.number = current + 1` | 200. Stale or skipped numbers: 409 "Version must be incremented". Two parallel writers on the same version: one 200, one 409. `minorEdit: true` was sent but versions later reported `minorEdit: false` |
| New draft | `POST` with `status: draft` | 200. Readable only with `?status=draft`; absent from search and child listings |
| Update draft | `PUT …?status=draft` with `status: draft` | 400 "Updating a draft without publishing is not possible" |
| Publish draft | `PUT /rest/api/content/{draftId}` (no query) with `status: current`, `version 1`, `space`, `ancestors` | 200, same ID. `PUT …?status=draft` with `status: current` returned 400 without `space`, 404 with it |
| Unpublished edit of a published page | Undocumented `POST /rest/tinymce/1/drafts` (`draftId`, `pageId`, `parentPageId`, `type`, `title`, `spaceKey`, `content`, `pageVersion`), found in editor JavaScript | 200 with `draftId,time,pageId`. Published body unchanged. Listed in `/users/viewmydrafts.action`, not readable by `GET /rest/api/content/{id}?status=draft` |
| Discard that edit | Undocumented `DELETE /rest/tinymce/1/drafts/discard` (`draftId`, `pageId`, `type`, `spaceKey`) | 200; removed from the drafts list |
| Footer comment and reply | `POST` with `type: comment`, `container`, reply `ancestors` | 200 |
| Inline comment | `POST` with `extensions.location: inline` | 400 "Could not create content with type comment". The UI uses the inline-comments plugin |
| Move | `PUT /rest/api/content/{id}/move/{position}/{target}` | 404: not available on 9.2.10 |
| Move | `PUT /rest/api/content/{id}` with new `ancestors` | 200; creates a page version. Children, attachments and comments follow |
| Reorder siblings | Undocumented `POST /pages/movepage.action?pageId&position=above&targetId&atl_token` | 200 JSON `commandDelegate`; explicit positions set; no page version |
| Rename | `PUT` with new title | Same ID. The tiny link redirects to the page; the old `/display/{space}/{title}` URL returned 404 |
| Trash | `DELETE /rest/api/content/{id}` | 204. `GET` 404; `?status=trashed` 200. A second `DELETE` returned 404, so it does not purge. The trashed page's child became a top-level page in the space, not a child of the grandparent |

Fixture text in the page body, comment text, attachment content, image alt text, and text inside
`code` and `expand` macros were all found by search within seconds. A renamed page was found by its
new title shortly afterwards. Remaining in the personal space: `Connector Lab`, `Connector Lab
Destination`, `Connector Lab Sample` (with child, attachments and comments) and `Connector Lab Draft`,
plus two trashed disposable pages. Not tested: restore and purge from trash, hierarchy deletion,
cross-space moves, inline-comment creation, and any write with a PAT.

### PAT on the same read routes

**2026-10-19 — Live, PAT:** a user-run standalone stdlib script with a bearer PAT, entered at a hidden
prompt, sent read-only `GET`s to the routes above. Results matched the browser session: current user
(`known`), the 500 cap with the requested `limit` echoed, `view`/`storage`/`export_view` bodies, the
`includeArchivedSpaces` difference, quick navigation, `child/attachment`, `child/comment?depth=all`,
the inline-comments plugin, the experimental version list, historical version 1, the page-tree action
and the macro browser all returned 200. `descendant/page` again returned 500.

The attachment download link (`_links.download`, redirects followed) returned 200 `text/html` with no
`Content-Disposition`, not the file. PAT access to `/download/attachments/…` is therefore unproven,
possibly a sign-in page. The final URL and body were not inspected.

Official schemas and CQL behavior remain at the
[Data Center REST reference](https://developer.atlassian.com/server/confluence/rest/latest/) and
[CQL guide](https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/).
Relative frontend SDK fragments can target another host; do not infer Wiki APIs from route-shaped
strings alone. Next investigations are tracked in [roadmap](roadmap.md), not duplicated here as a plan.
