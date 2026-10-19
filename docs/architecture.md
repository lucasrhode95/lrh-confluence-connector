# Confluence connector architecture

Current implementation, recorded 2026-10-18. Intended capabilities are in
[requirements](requirements.md); unbuilt work is in [roadmap](roadmap.md).

## 1. Entry points and lifetime

`confluence-connector` runs `confluence_connector.server:main` as one stdio MCP process per client
session. There is no daemon, scheduler, web surface or persistent content store.
`main.py` is a manual Basic-auth search demo, not an additional supported product surface.

The current implementation is read-only. Three tools create a fresh HTTP client for each call:

| Tool | Implemented behavior |
|---|---|
| `confluence_search_pages(query, limit=10)` | One page of page-only CQL `siteSearch` results; no caller-controlled paging or structured scope |
| `confluence_list_spaces(limit=50, start=0)` | One explicitly selected page of visible spaces |
| `confluence_get_page(page_id)` | Current page ID, title, space, version, link and `body.storage` markup |

There are no CRUD/move, tree, comments, download, export or metadata-batch tools yet.

## 2. Modules and boundaries

- `src/confluence_connector/server.py`: MCP declarations and startup configuration selection.
- `src/confluence_connector/client.py`: authentication selection, HTTP requests, validation and wire
  response mapping into dictionaries.
- `src/confluence_connector/config.py`: shared stdlib-only deployment-settings loader and validator.
- `research/probe_auth.py`: manual authentication/read probe; it reuses the configuration loader,
  not the application HTTP client. Secrets and cookies remain in memory.
- `tests/test_config.py`: synthetic configuration and fake-transport checks, including probe behavior.

The proof of concept does not yet have the service/remote/domain layers or typed content models used
in the mature connectors. Repository documentation normalization does not introduce those layers.

## 3. Configuration and authentication

The private configuration file exists because the remote Confluence Data Center server is
organization-hosted, with deployment-specific addressing and installation paths. The connector itself
runs locally; that process model is separate from how the remote product is hosted.

The required private JSON contains `base_url`, `query`, `form_auth_path` and `endpoints` for current
user, page search, UI search, spaces and content. The loader validates HTTPS targets and relative
endpoint paths, preserving installation context paths such as `/wiki`.

Selection precedence is `--config PATH`, `CONFLUENCE_CONFIG`, then repository-root
`confluence_config.json`, independently of the working directory. Parsed configuration is cached in
the process; restart after edits. Missing/invalid settings fail before authentication or network access.

Application credentials come from either `CONFLUENCE_PAT` or both `CONFLUENCE_USERNAME` and
`CONFLUENCE_PASSWORD`. Selecting both methods, neither, or incomplete Basic credentials fails.
There is no credential persistence, account binding, sign-out, shared token provider or cross-process
authentication coordination yet. The Microsoft authentication library is not used.

### 3.1 Token storage (decided, not built)

Decided 2026-10-19; the current code still reads environment credentials.

The token will be stored in an encrypted file in a per-user application folder, written through
`msal-extensions` (`build_encrypted_persistence`) and guarded by its `CrossPlatLock`. The library is
used only as a storage layer. It persists arbitrary bytes, so no MSAL sign-in or Microsoft token is
involved. It was chosen because:

- it encrypts with the operating system's user-scoped protection (DPAPI on Windows, the keychain or
  keyring elsewhere), with no key for us to manage;
- its file lock lets concurrent MCP processes and the CLI read and replace the token safely;
- `lrh-m365-auth` already uses it, so storage and locking behave the same across the `lrh-*`
  connectors.

Every Confluence call reads the token from storage under the lock, so a sign-out or replacement takes
effect in all running processes at their next call. Missing encryption is an error; there is no
plaintext fallback.

### 3.2 Harmonization with the Microsoft 365 connectors (decided, not built)

Decided 2026-10-19. The connector follows the organisation of `lrh-sharepoint-connector`,
`lrh-outlook-connector` and `lrh-teams-connector`:

- **Command line:** `confluence-connector auth`, `auth --sign-out`, `status [--check]` and `mcp`
  through `confluence_connector.__main__`, with the same exit codes, replacing `server.py` and
  `main.py`.
- **Process model:** a lazy composition root (`bootstrap.AppContext`) per MCP process holds one async
  `httpx` client, rebuilt when the stored account changes.
- **Layers:** `surfaces/` (MCP), `service/`, `remote/` (ports, transport, Confluence adapters and
  mapping) and `domain/` (errors, models), plus `config.py` and `logs.py`. Modules doing the same job
  as in the siblings (`logs.py`, `service/localfiles.py`, the error names) keep their names and
  behavior.
- **Local data:** `%USERPROFILE%\.lrh-confluence-connector` on Windows (outside AppData, which
  packaged apps redirect), the XDG data folder elsewhere, or `CONFLUENCE_CONNECTOR_HOME`.
- **Documentation:** this document links an overview diagram (`diagrams/architecture.svg`) and a code
  map (`diagrams/code-map.svg`), and the README opens with `connector-overview.svg`, as in the
  siblings.

## 4. Retrieval and errors

The client escapes a text query into a fixed CQL expression; it does not accept raw caller CQL.
Search text must be nonempty and no longer than 200 characters. Result limits are 1–100; space
offsets must be nonnegative; page IDs must be numeric. Search and space tools return compact results,
start, limit and size. They do not establish exhaustive coverage or provide a next cursor.

The HTTP client disables redirects and uses a 20-second request timeout with a 10-second connect
timeout. Login redirects, authentication denial, permission denial, transport errors and unexpected
JSON are reported as errors. No automatic retries are implemented.

Bodies are returned as storage markup, without macro resolution, text conversion or attachment
extraction. Link normalization and comprehensive response mapping still need verification.

## 5. Research and checks

The probe supports anonymous, PAT, Basic, form-session and supplied OAuth-bearer modes. Form mode makes
one interactive login attempt and tests immediate memory-only cookie replay. OAuth mode transports an
already obtained token; it does not implement authorization or registration. No probe runs on startup.

Run offline checks from the repository root:

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research main.py tests
```

Research findings are in [api-research.md](api-research.md). Research instructions and explicit live
commands are in [research/README.md](../research/README.md). No live calls are routine checks.
