# Confluence connector

Local MCP connector for one user's Confluence Data Center account. The current implementation is a
read-only proof of concept; the agreed product also includes page/tree reading, structured search,
exports, attachment retrieval, draft/page CRUD and moves. See the [documentation index](docs/README.md).

| Document | Purpose |
|---|---|
| [Requirements](docs/requirements.md) | Agreed scope, capabilities and ownership |
| [Architecture](docs/architecture.md) | Current code and tool contracts |
| [Roadmap](docs/roadmap.md) | Short register of open work and decisions |
| [API research](docs/api-research.md) | Dated evidence and limits |
| [Research toolkit](research/README.md) | Manual probes and UI investigation |

## Configure and run

Requires Python 3.12+ and `uv`. Run `uv sync` to prepare the environment. Copy
`confluence_config.example.json` to the ignored `confluence_config.json` and set the deployment URL,
relative API paths and synthetic probe query. There are no embedded deployment fallbacks.

The existing app accepts either a PAT in `CONFLUENCE_PAT`, or HTTP Basic credentials in both
`CONFLUENCE_USERNAME` and `CONFLUENCE_PASSWORD`. PAT access was verified in the recorded environment;
Basic acceptance was not. Credentials are not persisted. Encrypted persistent authentication and
logout are requirements, not implemented features.

```powershell
uv run confluence-connector --config ./confluence_config.json
```

For a local MCP client, use command `uv`, arguments `run`, `--directory`, the absolute project
folder, `confluence-connector`, `--config`, and the absolute private configuration path. Supply the
selected credentials through private process environment settings, never committed/shared config.

Configuration selection is `--config PATH`, then `CONFLUENCE_CONFIG`, then repository-root
`confluence_config.json`, independently of the working directory. Every command supports `--config`.
Restart after editing: validated settings are cached per process. API paths are relative to the
base URL, including an installation context such as `/wiki`.

## Current tools

- `confluence_search_pages(query, limit)`: one page of compact page-only CQL search results.
- `confluence_list_spaces(limit, start)`: one page of accessible spaces.
- `confluence_get_page(page_id)`: current page title, space, version, link and storage-format body.

All current tools are read-only. Bodies may contain macro markup. Structured scopes, tree traversal,
comments, downloads, exports and writes remain open work. The manual `main.py` Basic-auth demo is
retained for investigation; MCP is the only intended product surface.

## Research and checks

Live probes are manual and require authorization. The [research toolkit](research/README.md)
explains the modes; the [UI runbook](research/ui-runbook.md) starts with search and page reading.
Browser form login is an isolated research option, not the app's authentication backend.

Run offline checks from the repository root:

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research main.py tests
uv run python research/probe_auth.py --help
```

## Private data

Publish source and sanitized findings only. Keep deployment settings, tokens, cookies, page content,
HARs and downloaded resource bundles outside Git. Captures are temporary exploratory inputs outside
repositories; tests use synthetic content. No probe or application call consumes a capture.
