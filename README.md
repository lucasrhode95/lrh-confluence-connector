![Local MCP clients connected over stdio to the Confluence connector, which works with Confluence Data Center through its REST API and private page-move action.](https://raw.githubusercontent.com/lucasrhode95/lrh-confluence-connector/main/docs/connector-overview.svg)

A local MCP server for one user's Confluence Data Center account.

- Search pages by text, title, space, label, author and date, and find people.
- Read pages as agent-friendly text with typed references, page trees and full metadata.
- Download selected attachments and export pages, with their attachments, to local files.
- Create and update pages and drafts, manage labels, move pages, and trash or restore them.

## Requirements

- A Confluence Data Center account, and a personal access token you create in your Confluence profile
  (*Settings → Personal Access Tokens*). No administrator is needed.
- Permission in the spaces you want to read or change.

## Setup

1. **Install and connect.** Use the folder already open, or choose a folder if you have none.
   Reuse an existing clone when appropriate; keep it in place because the client launches it there.

   - **With a prompt:** give your local agent:
     `Install and configure https://github.com/lucasrhode95/lrh-confluence-connector following its README Setup section.`
     The agent handles preparation and configuration below, then hands you the sign-in.
   - **Manually:** install [Git](https://git-scm.com/downloads) and
     [uv](https://docs.astral.sh/uv/getting-started/installation/). uv supplies Python 3.12+ if needed.
     Find uv's full path with `(Get-Command uv -CommandType Application).Source` on Windows or
     `command -v uv` on macOS/Linux. Test that executable with `--version`, then clone and install:

     ```bash
     git clone https://github.com/lucasrhode95/lrh-confluence-connector.git
     cd lrh-confluence-connector
     uv sync --locked
     ```
   - **Codex CLI:** after manual preparation, register with
     [Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli):

     ```bash
     codex mcp add lrh-confluence-connector -- "/absolute/path/to/uv" run --directory "/absolute/path/to/lrh-confluence-connector" confluence-connector mcp
     ```
   - **Claude Code CLI:** after manual preparation, register with
     [Claude Code](https://code.claude.com/docs/en/mcp):

     ```bash
     claude mcp add --transport stdio lrh-confluence-connector -- "/absolute/path/to/uv" run --directory "/absolute/path/to/lrh-confluence-connector" confluence-connector mcp
     ```
   - **GitHub Copilot CLI:** after manual preparation, register with
     [Copilot](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers):

     ```bash
     copilot mcp add lrh-confluence-connector -- "/absolute/path/to/uv" run --directory "/absolute/path/to/lrh-confluence-connector" confluence-connector mcp
     ```
   - **GitHub Copilot in VS Code:** after manual preparation, merge this server into
     `.vscode/mcp.json`, preserving existing servers
     ([VS Code guide](https://code.visualstudio.com/docs/copilot/chat/mcp-servers)):

     ```json
     {
       "servers": {
         "lrh-confluence-connector": {
           "type": "stdio",
           "command": "/absolute/path/to/uv",
           "args": ["run", "--directory", "/absolute/path/to/lrh-confluence-connector", "confluence-connector", "mcp"]
         }
       }
     }
     ```

   Replace all sample paths with your actual executable and clone paths, including `uv.exe` on
   Windows. Use uv's full path in MCP configuration: desktop apps may have a different PATH from
   the terminal. In JSON, escape backslashes or use forward slashes.

2. **Configure your instance.** Copy `confluence_config.example.json` to `confluence_config.json` in the
   clone (it is git-ignored) and set `base_url` to your Confluence address, including any context path,
   for example `https://wiki.example.com` or `https://intranet.example.com/confluence`. The endpoint
   paths are relative to it and rarely need changing. Leave `cql_timezone` at `null`: sign-in measures
   it. `query`, `form_auth_path` and `acceptance` are used only by the
   [research tools](research/README.md).

   To keep the file elsewhere, pass `--config PATH` before the command (`confluence-connector --config
   PATH mcp`) or set `CONFLUENCE_CONFIG`.

3. **Sign in.** From the connector folder, run:

   ```bash
   uv run confluence-connector auth
   ```

   Paste your personal access token at the hidden prompt. An agent should hand this step to you and
   never ask for the token in chat. The connector checks the token with Confluence, stores it with your
   username in encrypted per-user storage (it refuses to run without encryption), and then measures
   Confluence's search time zone into `cql_timezone`. To switch accounts, run `auth --sign-out`, then
   `auth` again; sign-out deletes the local copy but does not revoke the token in Confluence.

4. **Restart the client completely.** Save your work and close **all** VS Code or chat-app windows.
   On Windows, check the system tray beside the clock, including hidden icons, and Quit/Exit any
   remaining client instance. Then reopen the app. A new chat or terminal is not a full restart.
   An agent should leave these steps for you before its session ends, rather than close the app.
   Restart again after any later change to `confluence_config.json`.

5. **Verify and try it.** Check the stored sign-in against Confluence from the connector folder:

   ```bash
   uv run confluence-connector status --check
   ```

   Then confirm the tools appear in the restarted client and make a small read-only request, such as
   "Find Confluence pages about the release checklist". An agent should report CLI and MCP verification
   separately, leaving any post-restart check as pending until it is performed.

Use uv's full path if the terminal cannot find it. In PowerShell, prefix a quoted executable path
with `&`, for example `& "C:/path/to/uv.exe" --version`.

### Local data

The encrypted token, downloads, exports and logs are stored under `%USERPROFILE%\.lrh-confluence-connector`
on Windows, or `~/.local/share/lrh-confluence-connector` on other systems. Set
`CONFLUENCE_CONNECTOR_HOME` to override it. Downloads and exports are removed after seven days.

> **Client compatibility:** These examples require a locally running agentic client that can launch a stdio MCP server. Hosted chat apps cannot start this local process, and this project provides no remote MCP endpoint. ChatGPT can still reach it through OpenAI's [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), where your plan and workspace allow custom MCP apps; files the server saves stay on the computer running the tunnel.

## MCP tools

The MCP server exposes the tools below. Each one's inputs, results and caveats are in
[architecture §8](docs/architecture.md#8-mcp-tools).

| MCP tool | Service module | Behavior |
|---|---|---|
| `auth_status` | [`bootstrap.py`](src/confluence_connector/bootstrap.py) | Report the stored account offline |
| `list_spaces` | [`service/spaces.py`](src/confluence_connector/service/spaces.py) | List visible current and archived spaces |
| `search_content` | [`service/search.py`](src/confluence_connector/service/search.py) | Search text or titles with structured filters and cursors |
| `find_users` | [`service/users.py`](src/confluence_connector/service/users.py) | Find people by display name, or former users by exact username |
| `get_page_tree` | [`service/tree.py`](src/confluence_connector/service/tree.py) | Read ordered child pages from a page or space root |
| `get_page_metadata` | [`service/metadata.py`](src/confluence_connector/service/metadata.py) | Read versions, labels, contributors and ancestors |
| `get_pages` | [`service/pages.py`](src/confluence_connector/service/pages.py) | Read pages as text or unchanged storage, with typed references, attachments and comments |
| `download_attachments` | [`service/attachments.py`](src/confluence_connector/service/attachments.py) | Save selected attachments of one page locally |
| `export_pages` | [`service/exports.py`](src/confluence_connector/service/exports.py) | Export up to 500 full pages as a file or ZIP, optionally with attachments |
| `create_page`, `update_page`, `set_labels` | [`service/writes.py`](src/confluence_connector/service/writes.py) | Create pages or drafts, replace a page body at an expected version, change labels |
| `list_drafts`, `publish_draft`, `delete_draft` | [`service/writes.py`](src/confluence_connector/service/writes.py) | List, publish or permanently delete new-page drafts |
| `move_page`, `delete_page`, `list_trash`, `restore_page` | [`service/structure.py`](src/confluence_connector/service/structure.py) | Move pages, trash them with their children, list the trash and restore with placement |

## Related connectors

The `lrh-*` connectors give local MCP agents the tools of a common corporate environment, one product
per repository. The Microsoft 365 connectors,
[lrh-outlook-connector](https://github.com/lucasrhode95/lrh-outlook-connector) (mail and calendar),
[lrh-teams-connector](https://github.com/lucasrhode95/lrh-teams-connector) (chats and meetings) and
[lrh-sharepoint-connector](https://github.com/lucasrhode95/lrh-sharepoint-connector) (files), share one
Microsoft sign-in through [lrh-m365-auth](https://github.com/lucasrhode95/lrh-m365-auth). This connector
has its own Confluence token and shares only the encrypted-storage technique and the command layout.

Confluence owns its pages, drafts, comments and attachments. A link from a page to a Teams chat, an
email or a SharePoint file stays a link; the agent follows it with the owning connector.

## Development

```bash
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research tests
uv run python research/probe_auth.py --help
```

Logs: each `mcp` process writes a daily-rotated file under `<data directory>/logs/`, kept for seven days. Set
`CONFLUENCE_CONNECTOR_LOG_LEVEL=DEBUG` for more detail ([architecture §7](docs/architecture.md#7-data-and-identity)).

Project references: [documentation index](docs/README.md) · [requirements](docs/requirements.md) · [architecture](docs/architecture.md) · [API research](docs/api-research.md) · [open roadmap](docs/roadmap.md) · [research toolkit](research/README.md).

## Disclaimer

> [!WARNING]
> Unofficial: this tool uses your personal access token and, for page moves and title matches, private
> Confluence web routes. Check your organization's AI and data policies before giving an agent your work
> data. Provided as is, without warranty. Atlassian and Confluence are Atlassian trademarks; this
> project is not affiliated with Atlassian.
