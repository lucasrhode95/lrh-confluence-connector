![Confluence connector overview](docs/connector-overview.svg)

# Confluence connector

Local MCP connector for one user's Confluence Data Center account. The current release provides
encrypted token setup, account status, space listing, content search, user lookup, page trees and page
metadata. The remaining product scope is in
the [requirements](docs/requirements.md) and [roadmap](docs/roadmap.md).

| Document | Purpose |
|---|---|
| [Requirements](docs/requirements.md) | Agreed scope, capabilities and ownership |
| [Architecture](docs/architecture.md) | Current code and implemented contracts |
| [Roadmap](docs/roadmap.md) | Open implementation and live-verification work |
| [API research](docs/api-research.md) | Dated evidence and limitations |
| [Research toolkit](research/README.md) | Explicit manual probes and UI investigation |

## Current MCP tools

- `auth_status` reports whether a token is stored and which account name it is bound to. It is offline;
  use the CLI's `status --check` to verify that Confluence still accepts the token.
- `list_spaces` lists current and archived spaces visible to the stored account, including their name,
  key, type, status and link when Confluence supplies one. It follows the server's actual
  `start + size` paging and requests up to 500 spaces at a time.
- `search_content` performs literal, scoped content or title searches with stateless continuation
  cursors. Archived spaces are included by default; title searches can add labelled quick-navigation
  results.
- `find_users` resolves current users by display name and former users by exact username.
- `get_page_tree` returns ordered children and ancestors from a page or space root with bounded depth.
- `get_page_metadata` returns history, retained versions, labels, contributors and ancestors for up
  to 20 page references.

Page text reading, exports, attachment retrieval, and page or draft changes remain open work.
Authentication is separate from Microsoft 365 sign-in.

## Setup

Requires Python 3.12+ and `uv`. Copy `confluence_config.example.json` to the ignored
`confluence_config.json`, then set the deployment URL and relative API paths. The probe query and form
login path are used only by the separate research toolkit.

From the repository root:

```powershell
uv sync --locked
uv run confluence-connector auth
uv run confluence-connector status --check
```

`auth` prompts for a personal access token without echoing it, checks it against the configured
current-user endpoint, then stores the token and account name in encrypted per-user storage. If
encrypted storage is unavailable, the command fails without a plaintext fallback. To clear the local
token, run `uv run confluence-connector auth --sign-out`; this does not revoke the token on the server.

Configuration precedence is `--config PATH`, `CONFLUENCE_CONFIG`, then the repository-root
`confluence_config.json`, independent of the working directory. Every command supports `--config`.
Validated settings are cached per process, so restart after editing. API paths are relative to the
configured base URL, including an installation context such as `/wiki`.

## MCP client configuration

Use command `uv` and these arguments, replacing both paths with absolute local paths:

```json
{
  "mcpServers": {
    "lrh-confluence-connector": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\path\\to\\lrh-confluence-connector",
        "confluence-connector",
        "--config",
        "C:\\path\\to\\lrh-confluence-connector\\confluence_config.json",
        "mcp"
      ]
    }
  }
}
```

## Research and checks

Live probes are manual and require authorization for the requested operations. The [research toolkit](research/README.md)
explains its modes; the [UI runbook](research/ui-runbook.md) starts with search and page reading.
Browser form login is an isolated research option, not the application authentication backend.

Run offline checks from the repository root:

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research tests
uv run python research/probe_auth.py --help
```

Keep deployment settings, tokens, cookies, page content, HARs and downloaded resource bundles outside
Git. Tests and fixtures use synthetic content only.
