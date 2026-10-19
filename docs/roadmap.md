# Confluence connector roadmap

Open work only. Settled requirements are in [requirements](requirements.md); the current implementation
is in [architecture](architecture.md); dated evidence is in [API research](api-research.md).
Delete items when complete. Snapshot **2026-10-18**.

## Implementation

- Expand the read-only proof of concept into structured search scopes, complete page/tree reads,
  batch metadata, textual extraction, comments and attachment references/retrieval.
- Add batch ZIP extraction with page/version selection and a parsing/fetching scope.
- Add page/draft CRUD and moves after verifying lifecycle and conflict semantics.
- Add encrypted persistent token storage, explicit logout and cross-process coordination.
- Establish the service/remote/domain boundaries, typed source metadata, reliable errors and safe
  retries needed by those capabilities; keep MCP as the only product surface.

## Decisions

- Use search probes to decide whether native search is sufficient; choose a local index/cache only
  if evidence warrants it. Search traversal, paging contracts and coverage limits remain open.
- Select enriched content representation, plain-text edit semantics, macro handling and export schema.
- Set draft/publish/delete behavior, cancellation and token storage/revocation contracts from evidence.
- Choose representative acceptance cases and practical operation/output limits.

## Live verification

- Read-only sessions and personal-space write tests ran on 2026-10-19 through a browser session (see
  [API research](api-research.md)). Still open: attachment download and writes with a PAT, search by a
  deactivated contributor, trash restore/purge, hierarchy deletion, inline-comment creation and cross-space moves.
- Decide whether unpublished edits of published pages (undocumented editor-draft routes) and sibling
  reordering (`movepage.action`) are worth supporting, since documented REST covers neither.
- Decide from that evidence: normalizing title and identifier punctuation, quoting or ANDing multi-word
  queries, `includeArchivedSpaces` via `rest/api/search`, `view` versus `storage` for reads, CQL
  `ancestor` in place of the failing `descendant/page`, and paging on `start + size`.
- Capture rich content, comments, attachment references, metadata and version behavior; then perform
  explicitly authorized disposable draft/page/move/delete tests, preferably in the personal space.
  Establish whether collaborative drafts are visible through documented REST and how stale-version
  edits conflict.
- Verify token lifecycle, identity/account safeguards, concurrent processes and eventual export behavior.

Live probes require authorization for the exact reads/writes performed. Resource bundles expose UI
code, not successful requests. Record dated results and limitations in the research record.
