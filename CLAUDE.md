# Repository agent instructions

`CLAUDE.md` (Claude Code) and `AGENTS.md` (Codex and other agents) must be identical. Change both together.

The first part, **Shared rules**, is the same in every `lrh-*` repository: change it in all of them at
once. The second part holds this repository's own rules.

# Shared rules

## Process and persistence

This is a local, single-user tool unless this repository documents otherwise. Keep processes scoped to
their documented entry point or session. Do not add schedulers, sync loops, watchers, warm-up tasks,
remote listeners, or multi-user features unless asked.

- Do not assume in-memory state survives across calls or processes. Return what a later call needs or
  read it from the server.
- Serve cached data only while it meets the documented freshness policy. If fresh data is required, wait
  for refresh instead of serving stale data.
- Shared local files (auth state, caches, indexes, output folders) must tolerate concurrent processes.

## Input boundaries and code structure

Validate untrusted input once at the entry point that receives it, and let internal routines trust the
validated contract. Document entry-point validation and internal assumptions in docstrings. When adding a
caller to a routine that assumes sanitized input, make sure that caller provides it.

Extract needed metadata from source fields and source structure (including HTML and card payloads)
before rendering. Carry those facts and ordered content references through mapping into typed domain
models. Do not reconstruct type, sender, ownership, origin, or attachment identity from flattened,
normalized, or exported text, or from fixed textual markers. Parsing source structure, searching content,
and transforming presentation are valid operations; rendered output must not become a metadata source.

Order modules top-down: public entry points first, then called functions, then private helpers and
formatting. Keep calls followable with named functions; prefer explicit branches over string-based
dispatch. Import the names used. Keep test-only seams in tests; constructor injection that is part of the
design stays. Modules that do the same job in several repositories (logging, local files, domain errors)
keep the same names and behavior; change them in every repository together.

## Checks, tests, and documentation

Run every check documented for this repository before each commit and fix failures. Never skip, disable,
or weaken a check to get a green result. Add or update tests for behavior changes and use synthetic
fixtures, never real captures or private user content. Update affected documentation in the same change,
and date new live research findings.

Documentation keeps each file to its own scope: requirements (what it must do, decisions, ownership) and
architecture (how it is built) are numbered; the research record holds only current findings that
Microsoft does not document or documents poorly; the roadmap holds open work only, split into
implementation, decisions and live verification. Keep documentation direct and summarized. Text files use
LF line endings (`.gitattributes`).

## Private data, external effects, and stored formats

Never log or commit credentials, tokens, session cookies, private user content, or real identifiers. Use
this repository's documented secure storage for authentication state. Logs carry operational timings,
statuses and counts only. Do not perform external writes without the user's explicit request. Retry a
write automatically, a bounded number of times and honoring `Retry-After`, only when the failure proves it
was not applied: the request was never sent, or the service answered 429 or 503. Never resend a write whose
outcome is uncertain (sent without a complete answer, or answered 504); report it so it can be checked.
When a multi-step write fails, undo the steps it already completed where that is safe, and report what
could not be undone. Resuming a partial upload from the ranges the service reports missing is allowed.

Treat persisted data as the current format. Do not add backward-compatibility shims, old-format readers,
automatic migrations, schema-version frameworks, or fallback behavior for previous application versions
unless asked. Keep safeguards that protect current user data and account ownership.

## Research scope

Research probes make explicitly authorized requests to configured endpoints and report responses. They do
not consume browser captures. HARs, Resource Saver archives and downloaded source bundles are temporary
inputs for ad hoc exploratory work outside the repository. Document useful URLs, request/response shapes,
dated findings and limitations. Do not retain raw or anonymized captures, per-file/member manifests, source
indexes, or capture-reading, regeneration and redaction machinery in the repository. Probes must work
without captures.

## Portability

Keep source, documentation, comments and fixtures free of real personal names, account addresses,
customer/project names and organization identifiers. Use fictional examples and synthetic content.
Deployment-specific URLs, clients, scopes and any expected-account checks belong in named configuration
settings. Microsoft public endpoints and first-party client IDs may be baseline settings; keep regional
service discovery and account-ownership checks.

The research record is an anonymized, dated starting point for forks and future policy changes. State
which client, resource, operation and evidence support a finding, without identifying the account or
organization. One environment's grants, denials or empty responses are not universal restrictions;
reassess capabilities before choosing a backend for a deployment. Flag genuinely organization-exclusive
endpoints or fixed-account dependencies for the user's decision before changing them. Standalone research
scripts require an explicit private config file rather than embedded fallback settings.

# Confluence connector rules

## The project

A local MCP connector for one user's Confluence Data Center account. Requirements include search,
full page/tree reads, metadata, attachment retrieval, exports, and draft/page CRUD and moves.
The current MCP surface implements `auth_status`, `list_spaces`, `search_content`, `find_users`,
`get_page_tree`, `get_page_metadata`, `get_pages`, `download_attachments`, `export_pages`,
`create_page`, `update_page`, `list_drafts`, `publish_draft`, `delete_draft`, `set_labels`,
`move_page`, `delete_page`, `list_trash` and `restore_page`. Do not describe unimplemented roadmap
work as implemented.
Confluence credentials are separate from the Microsoft OAuth library.

- Documentation index: `docs/README.md`
- What it must do: `docs/requirements.md`
- How it is built today: `docs/architecture.md`
- API/authentication evidence: `docs/api-research.md`
- Open work: `docs/roadmap.md`
- Probes and UI investigation: `research/README.md`

`__main__.py` owns CLI entry points, `surfaces/mcp_main.py` owns MCP declarations,
`remote/transport.py` owns HTTP behavior, and `config.py` owns private settings. Add layers only when
they support a product capability, and keep `docs/architecture.md` aligned with the code.

## Authentication and configuration

Use a user-generated token without requiring administrator registration. The application prompts
for a PAT without echoing it, verifies it against the configured current-user endpoint, and stores
the token with its username in encrypted `msal-extensions` persistence under `CrossPlatLock`.
Environment credential modes belong only to the separate research probe. Never reuse Microsoft SSO
as if it were a Confluence OAuth grant.

App and probes use a required ignored private JSON file and one loader. Selection precedence is
`--config PATH`, `CONFLUENCE_CONFIG`, then repository-root `confluence_config.json`; no embedded
instance, query or endpoint fallbacks. Configuration is cached per process; restart after editing.
Keep deployment/account settings private and all examples synthetic.

## Research, ownership and safety

Prefer documented Atlassian APIs; investigate undocumented browser/plugin methods when a required
capability has no suitable documented path. Keep their source-only leads separate from verified
standalone requests. The undocumented form-login endpoint stays isolated to the interactive probe,
not the product authentication backend.

The shared research-record rule applies to Atlassian findings in this repository. Keep only dated
integration evidence and poorly documented contracts, with links for public reference material.
Do not retain capture/member inventories, source indexes or capture-processing machinery.

Confluence owns its content and attachments, not unrelated Jira, Microsoft or Git hosting data.
Agents handle interpretation and cross-product reasoning. Preserve source metadata and attachment
references before rendering; output markers are presentation, never an identity source.

Planned write support is not permission to mutate live content. Live probes require explicit
authorization for the actual operations and targets. Use disposable synthetic pages for authorized
write tests. Do not run live authentication or Wiki probes as routine checks.

## Checks

Run both offline checks before every commit:

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research tests
```

After moving or changing the probe, also check its CLI without network access:

```powershell
uv run python research/probe_auth.py --help
```

Tests use synthetic identities/configuration and fake HTTP responses only. Keep AGENTS.md and
CLAUDE.md identical, and update the affected documentation with behavior changes.
