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

Official schemas and CQL behavior remain at the
[Data Center REST reference](https://developer.atlassian.com/server/confluence/rest/latest/) and
[CQL guide](https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/).
Relative frontend SDK fragments can target another host; do not infer Wiki APIs from route-shaped
strings alone. Next investigations are tracked in [roadmap](roadmap.md), not duplicated here as a plan.
