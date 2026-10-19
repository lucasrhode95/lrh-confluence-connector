# Confluence 9.2 UI investigation runbook

Prepared 2026-10-18 for Data Center 9.2.10. This is a manual capture procedure, not evidence that the
actions already succeeded. Start with sessions 0 to 2. Use disposable pages for mutations.

## Approach

Documented REST comes first. Where a session reads content, also open the matching documented `GET`
in the same browser tab (read-only, browser session) and note whether it answers the question. UI
capture then targets what REST leaves unsettled: UI search ranking, collaborative drafts, action
endpoints for moves/deletes, and macro rendering. Writes through REST are separate, explicitly
authorized probes.

For every observed UI request, note the documented REST equivalent, if any, and the gap. That pairing
is the useful finding; the capture itself is not.

## Recording each session

Open browser Developer Tools → Network before acting. Enable Preserve log and Disable cache;
clear the log once per session. Keep all request types, including document, Fetch/XHR and WebSocket.
Wait for visible completion between actions. Save a HAR with response content (sanitized export
where offered), and use Save All Resources at the requested UI checkpoints.

Keep recordings outside Git in a private folder. Even sanitized HARs can contain private content and
`atl_token` values in URLs or form bodies. Do not record credentials, sign-in or token creation.
Maintain private notes with session/action, approximate time, expected/actual result and page link.
Record missing permissions/options instead of bypassing them. Inspect an uncertain write outcome
before repeating it. Note any 429 or `Retry-After` response. Delete captures once their findings are
summarized.

## Test location

Prefer your personal space for the fixture. You are normally its space administrator there, which
allows restoring or purging your own fixture pages, and it avoids notifying team-space watchers. Leave
"notify watchers" unchecked when publishing. Use a team space only if the personal space lacks a
needed feature, and only in an approved location.

## Sessions

| Session | Actions | Resource checkpoint |
|---|---|---|
| 0. Instance profile | Record whether the editor shows collaborative editing (other editors' avatars, autosave without a Save draft button). Open the macro browser and note installed app/macro categories. Note the help menu's version and whether the profile shows Personal Access Tokens. No content changes. | Macro browser |
| 1. Existing-content search | Select known pages with a distinctive title, body phrase and contributor. Compare quick and advanced search; paginate; test individual and combined space, title, creator/contributor, label, date, parent/ancestor filters. Include archived/personal spaces, comment/attachment types and a negative query. Record known-hit rank or omission. | Advanced search with filters open |
| 1b. Query behaviour | Same known pages: accented versus unaccented Portuguese words, plural/stemmed forms, word prefix, quoted phrase, case, and identifiers with hyphens, dots or underscores. Search text that sits only in a code block, panel, excerpt include or attachment content. Filter by a contributor whose account is deactivated, if one is known. Page deep into a broad query and compare reported total with reachable results. | HAR sufficient |
| 2. Reading and trees | Read representative pages with tables, code, panels, images, attachments, comments and macros. Scroll fully; expand sections/replies/resolved discussions; open image previews; navigate sidebar branches, children and ancestors. Note content loaded only after interaction, and tree branches hidden by restrictions. Note Jira, diagram, page-properties, include and excerpt macros. | Rich page after interactions |
| 2b. Representations | For one rich page, open `/rest/api/content/{id}` with `expand=body.storage`, then `body.view`, `body.export_view` and `body.editor`; also its `child/attachment` and `child/comment` routes. Compare each with the rendered page: resolved macros, image/attachment references, inline-comment anchors in storage. Open "View Storage Format" or "View Source" if the page menu offers them. | HAR sufficient |
| 3. Create fixture | Create two test parents, a child and grandchild. Add headings, lists, table, code/panel, internal link, synthetic image/alt text, text attachment and label. Try Expand/Table of Contents macros if available. Publish and verify after reload. Inspect any source/markup control without assuming HTML support. | Editor, macro controls and published page |
| 4. Draft lifecycle | Create an unpublished draft, close after autosave, find it through Recently worked on/Profile → Drafts, reopen/edit/close/reopen, then publish. Separately save unpublished edits on an existing test page, compare published/editor views, inspect changes and revert. Compare the editor with `GET /rest/api/content/{id}?status=draft`. Delete another disposable draft. Keep WebSocket frames: collaborative drafts may live outside REST. | Draft list and editor options |
| 4b. Concurrent edits | Open the fixture child in two tabs. Edit and publish in the first, then in the second. Record whether the editors merge, warn or conflict, and which version numbers result. | HAR sufficient |
| 5. Details | Add/reply to page and inline comments; resolve/view a discussion. Open attachments, preview/download synthetic files, inspect available versions. Open page information/history, publish an edit with a version comment, compare versions and view an old one. Note the page menu's export options (PDF, Word) without exporting real pages. | Attachments and history/details |
| 6. Move | More options → Move the test child to the other test parent; verify grandchild, comments, attachments and original URL. Move back. Reorder disposable siblings if permitted. Rename/publish and inspect old title URLs and the short `/x/` link. Confirm the page ID is unchanged. Optional cross-space move only between approved test locations. | Move dialog |
| 7. Controlled search | Search fixture title, unique body term, attachment filename/content and comment term. Test contributor/label/parent/ancestor filters, old/new title and broad real queries. Record elapsed time for indexing delays. | HAR sufficient |
| 8. Delete | Delete only a disposable parent and inspect surviving child placement. Separately test hierarchy deletion if offered. Inspect old URLs. Restore or purge only your own fixture pages, where you hold the permission. | Delete confirmation |

Suggested synthetic fixture prefix: `Connector Lab`; body marker: `orchidbeacon`; attachment
`connector-lab-notes.txt` containing `cedarattachment`. Add a unique suffix per run.

Confluence distinguishes new drafts from unpublished edits. Draft deletion bypasses trash. Published
page restoration requires space-admin permission. Never delete or discard someone else's draft edits.
Leave the main fixture for later authorized API probes and note what remains; clean up extra drafts
and disposable branches only.

## Handoff

Provide one HAR per session, resource ZIPs from the checkpoints, and private action notes. Keep the
existing home-page bundle as a source input. A resource bundle alone does not establish requests,
payloads, status codes or successful operations. Useful sanitized findings go to
[API research](../docs/api-research.md) as UI request → documented REST equivalent → gap; do not copy
private notes or capture contents into tests/docs.

Official versioned guides: [search](https://confluence.atlassian.com/conf92/search-1477576557.html),
[drafts](https://confluence.atlassian.com/conf92/drafts-1477575730.html),
[moves](https://confluence.atlassian.com/conf92/move-and-reorder-pages-1477575698.html),
[deletion](https://confluence.atlassian.com/conf92/delete-or-restore-a-page-1477575720.html).
REST schemas: [Data Center REST reference](https://developer.atlassian.com/server/confluence/rest/latest/).
