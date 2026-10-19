# Confluence 9.2 UI investigation runbook

Prepared 2026-10-18 for Data Center 9.2.10. This is a manual capture procedure, not evidence that the
actions already succeeded. Start with sessions 1 and 2. Use disposable pages for mutations.

## Recording each session

Open browser Developer Tools → Network before acting. Enable Preserve log and Disable cache;
clear the log once per session. Keep all request types, including document, Fetch/XHR and WebSocket.
Wait for visible completion between actions. Save a HAR with response content (sanitized export
where offered), and use Save All Resources at the requested UI checkpoints.

Keep recordings outside Git in a private folder. Even sanitized HARs can contain private content.
Do not record credentials, sign-in or token creation. Maintain private notes with session/action,
approximate time, expected/actual result and page link. Record missing permissions/options instead
of bypassing them. Inspect an uncertain write outcome before repeating it.

## Sessions

| Session | Actions | Resource checkpoint |
|---|---|---|
| 1. Existing-content search | Select known pages with a distinctive title, body phrase and contributor. Compare quick and advanced search; paginate; test individual and combined space, title, creator/contributor, label, date, parent/ancestor filters. Include archived/personal spaces, comment/attachment types and a negative query. Record known-hit rank or omission. | Advanced search with filters open |
| 2. Reading and trees | Read representative pages with tables, code, panels, images, attachments, comments and macros. Scroll fully; expand sections/replies/resolved discussions; open image previews; navigate sidebar branches, children and ancestors. Note content loaded only after interaction. | Rich page after interactions |
| 3. Create fixture | In an approved location, create two test parents, a child and grandchild. Add headings, lists, table, code/panel, internal link, synthetic image/alt text, text attachment and label. Try Expand/Table of Contents macros if available. Publish and verify after reload. Inspect any source/markup control without assuming HTML support. | Editor, macro controls and published page |
| 4. Draft lifecycle | Create an unpublished draft, close after autosave, find it through Recently worked on/Profile → Drafts, reopen/edit/close/reopen, then publish. Separately save unpublished edits on an existing test page, compare published/editor views, inspect changes and revert. Delete another disposable draft. | Draft list and editor options |
| 5. Details | Add/reply to page and inline comments; resolve/view a discussion. Open attachments, preview/download synthetic files, inspect available versions. Open page information/history, publish an edit with a version comment, compare versions and view an old one. | Attachments and history/details |
| 6. Move | More options → Move the test child to the other test parent; verify grandchild, comments, attachments and original URL. Move back. Reorder disposable siblings if permitted. Rename/publish and inspect old links. Optional cross-space move only between approved test locations. | Move dialog |
| 7. Controlled search | Search fixture title, unique body term, attachment filename/content and comment term. Test contributor/label/parent/ancestor filters, old/new title and broad real queries. Record elapsed time for indexing delays. | HAR sufficient |
| 8. Delete | Delete only a disposable parent and inspect surviving child placement. Separately test hierarchy deletion if offered. Inspect old URLs. Restore only if already permitted; never purge trash. | Delete confirmation |

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
[API research](../docs/api-research.md); do not copy private notes or capture contents into tests/docs.

Official versioned guides: [search](https://confluence.atlassian.com/conf92/search-1477576557.html),
[drafts](https://confluence.atlassian.com/conf92/drafts-1477575730.html),
[moves](https://confluence.atlassian.com/conf92/move-and-reorder-pages-1477575698.html),
[deletion](https://confluence.atlassian.com/conf92/delete-or-restore-a-page-1477575720.html).
