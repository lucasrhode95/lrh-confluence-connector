# API and frontend findings

Date: 2026-10-07. Full routes/provenance are in [ENDPOINTS.md](ENDPOINTS.md), endpoints.csv and source-occurrences.csv. HTTP methods below are captured HAR methods unless marked user probe or source-only.

## Standalone read access

User-run PAT and two form-session probes return 200 for current user (`/rest/api/user/current`), CQL content search (`/rest/api/content/search`), UI search (`/rest/api/search`), spaces (`/rest/api/space`) and one page (`/rest/api/content/{id}`). Identity is present; search and space calls return one result under limit=1. Anonymous probes return 401 for current user, both searches and spaces. Immediate copied-cookie replay succeeds; process/reboot persistence is unproven.

Some standalone endpoints are only represented by source/link templates in the captures. In particular, current-user lookup is a probe endpoint; do not infer its captured use from a successful user-run probe. Captured plugin reads are not automatically PAT-proven.

## Captured contracts

| GET route | Query key names | Response object key names / shape |
|---|---|---|
| `/rest/api/content/search` | `cql,cqlcontext,expand,_` | `_links,cqlQuery,limit,results,searchDuration,size,start,totalSize` |
| `/rest/api/search` | `cql,excerpt,expand,includeArchivedSpaces,limit,src,start` | Same search envelope |
| `/rest/experimental/search` | `cql,expand,limit,_` | Same search envelope |
| `/rest/quicknav/1/search` | `limit,query,src,type` | `contentNameMatches,query,queryTokens,totalSize` |
| `/rest/spacedirectory/1/search` | `filterUnauthorizedSpaces,pageSize,query,startIndex,status,type,_` | `spaces,totalSize` |
| `/rest/dashboardmacros/1.0/updates` | `category,labels,maxResults,showProfilePic,spaceKey,spaces,tab,types,users` | `changeSets,tabKey` |
| `/rest/recentlyviewed/latest/recent` | `limit` | No object envelope asserted |
| `/rest/ia/latest/spacesmenu/recent` | `limit` | No object envelope asserted |
| `/rest/ia/1.0/spacesmenu` | `_` | `template` |
| `/rest/inlinecomments/1.0/comments` | `containerId,_` | No object envelope asserted |
| `/rest/likes/1.0/content/{id}/likes` | `commentLikes,_` | `commentLikes,content_id,content_type,likes` |
| `/rest/watch-button/1.0/watchState/{id}` | `_` | `isAdmin,isBlogPost,watchingBlogs,watchingPage,watchingSpace` |
| `/rest/mywork/latest/status/notification/count` | `pageid,_` | `count,maxTimeout,timeout` |
| `/rest/jira-metadata/1.0/metadata/aggregate` | `pageId,_` | `count,incomplete,singleEntity` |
| `/rest/shortcuts/latest/shortcuts/{id}/{opaque}` | `_` | Success `shortcuts`; errors `message,status-code,sub-code`; 200 and 401 observed |
| `/plugins/pagetree/naturalchildren.action` | `pageId,ancestors,sort,startDepth,expandCurrent` and other tree options | HTML/UI response |
| `/plugins/macrobrowser/browse-macros.action` | `detailed` | `categories,editTitle,insertTitle,macros,title` |
| `/plugins/editor-loader/editor.action` | Page/editor options including `pageId,parentPageId,spaceKey,atl_token` | Editor HTML/resources; no captured token values |

The standalone probe also supplies content-search `limit=1,expand=space,version`. UI search uses a page/siteSearch CQL expression and `start=0,src=next.ui.search,expand=space.icon,includeArchivedSpaces=false`. These are probe parameters; actual query/page/space values are omitted from research.

Captured `POST /rest/wrm/2.0/resources` has request keys `exclude,interaction,require` and response keys `interaction,require`. It loads frontend resources. `/rest/cql/contenttypes` takes `category`; feature discovery, help tips and highlighting provide UI support.

## Discovered families (orientation to the full catalog)

| Family | Representative literal/template routes | Interpretation |
|---|---|---|
| Content/attachments/users | `/rest/api/content`, `/rest/api/content/{dynamic}/child/attachment`, `/rest/api/space`, `/rest/api/user`, `/rest/api/user/watch/content/` | Standard-looking bases plus source concatenations; only the five standalone reads above are PAT-proven |
| Search/history/dashboard | `/rest/cql/fields`, `/rest/cql/expressions`, `/rest/recentlyviewed/1.0/recent/pages`, `/rest/popular/1/stream/content`, `/rest/recommender/1.0/stream/content` | Mix of browser requests, generic/unused frontend features |
| Comments/likes | `/rest/inlinecomments/1.0/comments`, `/rest/tinymce/1/content/{dynamic}/comments/{dynamic}`, `/rest/files/1.0/files/content/{0}/commentCount` | Source templates and captured reads; mutations untested |
| Attachment upload/conversion | `/download/attachments/{id}/{filename}`, `/rest/prototype/1/content/{dynamic}/attachments.json`, `/rest/documentConversion/latest/conversion/thumbnail/`, `/plugins/drag-and-drop/upload.action` | Browser downloads captured; source upload/conversion routes not independently exercised |
| Favorites/watches | `/json/addwatch.action`, `/json/removewatch.action`, `/json/listwatchers.action`, `/rest/experimental/relation/user/current/favourite/toContent/{dynamic}`, `/pages/watch-tree.action` | Modification paths found, not executed |
| Restrictions/access requests | `/rest/access/latest/page/restriction/{dynamic}/check/{dynamic}`, `/rest/access/latest/page/restriction/{dynamic}/request/edit`, `/rest/access/latest/page/restriction/{dynamic}/grant/{dynamic}` | Reconstructed source URLs; no permissions changed |
| Blueprints/editor/macros | `/rest/create-dialog/1.0/blueprints/web-items`, `/rest/create-dialog/1.0/storage/quick-create`, `/rest/tinymce/1/macro/preview`, `/rest/tinymce/1/drafts/discard` | Shared editor source includes write operations; none tested |
| Page trees/copy/delete/export | `/pages/movepage.action`, `/pages/reorderpages.action`, `/plugins/tree-copy/preparing-copying.action`, `/plugins/tree-delete/preparing-delete.action`, `/spaces/{space}/pdfpageexport.action` | UI routes include mutations; no authorization to execute implied |
| Jira/app links | `/rest/jira-integration/1.0/issues`, `/rest/jiraanywhere/1.0/jira/appLink/{dynamic}/filter/{dynamic}`, `/plugins/servlet/applinks/proxy`, `/rest/applinks/1.0` | Cross-product targets/auth may differ from the Wiki |
| Installed plugins | `/rest/git-plugin/1.0/repositories/{dynamic}/commits`, `/rest/numberedheadings/1.0/page-configuration/{dynamic}`, `/rest/multiexcerpt/1.0/names`, `/rest/roadmap/1.0/bar/{dynamic}/status` | Source presence does not establish enabled/supported API access |
| Collaboration | `/rest/synchrony/1.0/content/`, `/rest/synchrony/1.0/token/`, `/rest/quickreload/latest/` | Token/collaboration fragments; no credentials retained or tested |
| UI/settings/admin | `/rest/feature-discovery/1.0/discovered/`, `/rest/atlassian-theme/1/user-preferences`, `/rest/licensebanner/1.0/reminder/later`, `/plugins/servlet/audit/resource/Space,{id}` | UI/source evidence; no admin rights inferred |
| Calendar/invitations | `/rest/calendar-services/1.0`, `/plugins/servlet/team-calendars/caldav/`, `/rest/easyuser/latest/sendUserInvites` | Principally shared HAR-source findings, possibly unused/unavailable |

## Limits and next questions

The large ZIP editor bundle includes mutation routes even though our connector/probes read content only. Unknown code-only HTTP methods stay unknown; reviewed auth methods are recorded separately. A route-shaped literal may be incomplete, an example or SDK/module reference. Relative `/api/v2/frontend/...` SDK paths can target another host at runtime; no Wiki API availability is asserted.

Use CQL/content/space APIs as the established reads; verify plugin APIs only when a capability requires one. Official API schemas, permissions and pagination stay at their source: [Confluence DC REST reference](https://developer.atlassian.com/server/confluence/rest/latest/) and [CQL guide](https://developer.atlassian.com/server/confluence/advanced-searching-using-cql/).

Pending: search recall, pagination, expanded/body formats, attachment APIs, token expiry/revocation, native OAuth rights, account ownership and encrypted persistence. No live Wiki requests or content mutations were made during this pass. Sources were treated as data; raw bodies/titles/identifiers/secrets were not copied.
