# Repository agent instructions

`CLAUDE.md` (Claude Code) and `AGENTS.md` (Codex and other agents) must be identical. Change both together.

## Shared development rules

### Process and persistence

This is a local, single-user connector unless this repository documents otherwise. Keep processes scoped to their documented entry point or session. Do not add schedulers, sync loops, watchers, warm-up tasks, remote listeners, or multi-user features unless asked.

- Do not assume in-memory state survives across calls or processes. Return what a later call needs or read it from the server.
- Serve cached data only while it meets the documented freshness policy. If fresh data is required, wait for refresh instead of serving stale data.
- Shared local files (auth state, caches, indexes, output folders) must tolerate concurrent processes.

### Input boundaries and code structure

Validate untrusted input once at the entry point that receives it, and let internal routines trust the validated contract. Document entry-point validation and internal assumptions in docstrings. When adding a caller to a routine that assumes sanitized input, make sure that caller provides it.

Order modules top-down: public entry points first, then called functions, then private helpers and formatting. Keep calls followable with named functions; prefer explicit branches over string-based dispatch. Import the names used. Keep test-only seams in tests; constructor injection that is part of the design stays.

### Checks, tests, and documentation

Run every check documented for this repository before each commit and fix failures. Never skip, disable, or weaken a check to get a green result. Add or update tests for behavior changes and use synthetic fixtures, never real captures or private user content. Update affected documentation in the same change, and date new live research findings.

### Private data, external effects, and stored formats

Never log or commit credentials, tokens, session cookies, private user content, or real identifiers. Use this repository's documented secure storage for authentication state. Do not perform external writes without the user's explicit request. Make each external write once; do not retry automatically, and report uncertain outcomes.

Treat persisted data as the current format. Do not add backward-compatibility shims, old-format readers, automatic migrations, schema-version frameworks, or fallback behavior for previous application versions unless asked. Keep safeguards that protect current user data and account ownership.

## The project

A read-only, local MCP connector proof of concept for one user's Confluence Data Center account.

- How to run and configure it: `README.md`
- Authentication findings: `AUTH-INVESTIGATION.md` and `research/AUTHENTICATION.md`
- API and capture research: `research/README.md`, `research/API-FINDINGS.md`, and `research/ZIP-ANALYSIS.md`

The MCP server is in `src/confluence_connector/server.py`; `src/confluence_connector/client.py` owns REST wire formats. The current connector reads through Confluence's REST API and accepts a PAT or HTTP Basic credentials from local environment configuration. It does not persist credentials.

## Confluence-specific safety

- Keep the connector read-only unless the user explicitly requests a specific write operation.
- Use documented REST APIs in the connector. The undocumented browser form-login endpoint is isolated to the interactive research probe; do not make it the connector's authentication path.
- Never commit raw HAR files, Resource Saver archives, browser session data, tokens, or page content. Keep research catalogs sanitized and use synthetic data for tests.
- Do not run live authentication probes or access the Wiki as part of routine code checks. The probe prompts for secrets locally and keeps them in memory.

## Checks and research

The project has no formal test suite yet. Before committing, run:

```powershell
uv run python -m compileall -q src
```

When the local capture bundle is available outside the repository, the research checks can also be run without printing captured values:

```powershell
uv run python research/probes/check_redaction.py --captures-dir <local-capture-folder> --research-dir research
```

Keep raw captures outside Git. Regeneration requires all five named captures; write generated outputs to a temporary directory and compare them with the checked-in research before replacing anything.