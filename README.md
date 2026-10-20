![Confluence connector overview](docs/connector-overview.svg)

A local MCP server for one user's Confluence Data Center account.

- Search pages and find users, then read page text, metadata and ordered trees.
- Download selected attachments and export pages to local files.
- Create and update pages, manage drafts and labels, move pages, and trash or restore pages.
- Keep authentication encrypted locally; the connector has no hosted service or background process.

## Requirements

- A Confluence Data Center account and a user-generated personal access token.
- Python 3.12+ and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Access to the target spaces and permissions for any requested writes.

## Setup

1. **Install the connector.** Clone the repository, then install its locked dependencies:

   ```bash
   git clone https://github.com/lucasrhode95/lrh-confluence-connector.git
   cd lrh-confluence-connector
   uv sync --locked
   ```

2. **Configure the instance.** Copy `confluence_config.example.json` to the ignored
   `confluence_config.json`. Set the deployment URL and its relative API paths. The required `query`
   and `form_auth_path` are used by the separate research probe. Optional settings:

   - `cql_timezone`: an IANA timezone or fixed `UTC±HH:MM` offset. `auth` or `status --check`
     measures the effective CQL offset when this is unset. The automatically written fixed offset
     will not follow daylight saving; replace it with an IANA timezone if needed.
   - `acceptance.personal_space_key` and `acceptance.cross_space_key`: explicit approved spaces for
     the manual synthetic acceptance fixture. The cross-space key must be different from the
     personal-space key.

   Configuration can also be selected with `--config PATH` or `CONFLUENCE_CONFIG`. The checked-in
   example contains only synthetic values; never commit private settings or credentials.
   Validated settings are cached in each process, so restart MCP after configuration edits or after
   CLI timezone calibration. API paths are relative to the configured base URL, including an
   installation context such as `/wiki`.

3. **Authenticate.** Run these commands from the repository folder:

   ```bash
   uv run confluence-connector auth
   uv run confluence-connector status --check
   ```

   `auth` prompts for the token without echoing it, verifies the account and stores the credential
   in encrypted per-user storage. It then calibrates the CQL timezone with read-only searches.
   Calibration runs only when you invoke `auth` or `status --check`, never when MCP starts. If
   encryption is unavailable, the connector refuses to run. `auth --sign-out` removes the local
   token; it does not revoke it in Confluence.

   Date-only search bounds mean the full calendar day in the configured Confluence timezone.
   ISO-8601 timestamps with offsets are instants; timestamps without an offset mean UTC. Timestamp
   filters need a configured `cql_timezone`.

4. **Configure an MCP client.** Use the absolute paths to `uv` and this checkout. For example:

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

   Replace the sample paths. Use the full path to `uv.exe` if the desktop client has a different
   `PATH` from your terminal; JSON paths may use escaped backslashes or forward slashes.

## MCP tools

Sign-in is a CLI command. The MCP server exposes these tools; inputs and response details are in the
[architecture guide](docs/architecture.md#6-mcp-tools).

| Tool | Behavior |
|---|---|
| `auth_status` | Report the stored account without contacting Confluence. |
| `list_spaces` | List visible current and archived spaces. |
| `search_content` | Search content or titles with structured filters and continuation cursors. |
| `find_users` | Find users by display name or exact username. |
| `get_page_tree` | Read ordered page descendants and ancestors. |
| `get_page_metadata` | Read page history, versions, labels, contributors and ancestors. |
| `get_pages` | Read text or unchanged storage markup with typed references and optional sections. |
| `download_attachments` | Save selected attachment IDs from one page to local storage. |
| `export_pages` | Export pages as text or rich HTML, optionally with attachment bytes. |
| `create_page` | Create a current page or new-page draft at a space root or under a parent. |
| `update_page` | Replace a page body with an explicit expected version and content-loss checks. |
| `list_drafts` | List the signed-in user's new-page drafts. |
| `publish_draft` | Publish a new-page draft without changing its page ID. |
| `delete_draft` | Permanently delete a new-page draft, bypassing the trash. |
| `set_labels` | Add or remove page labels and report partial outcomes. |
| `move_page` | Move a page between parents or spaces and optionally set sibling order. |
| `delete_page` | Trash a page and optionally move up or trash its children. |
| `list_trash` | List trashed pages for a space or across visible spaces. |
| `restore_page` | Restore a page and report its resulting placement. |

Page and structure writes change Confluence content. The connector checks permissions and reports
partial or uncertain outcomes; it does not retry a write whose result is unknown.

## Research and acceptance

The [research toolkit](research/README.md) documents manual probes, synthetic fixture setup, and
the opt-in live acceptance runner. Neither acceptance script makes network requests by default.
Fixture setup requires `--apply`; live checks require `--run-live`; Confluence write checks also
require `--allow-writes`. The fixture uses synthetic titles and content in the configured spaces.

Run the offline checks from the repository root:

```powershell
uv run python -m unittest discover -s tests
uv run python -m compileall -q src research tests
```

See [requirements](docs/requirements.md), [architecture](docs/architecture.md),
[roadmap](docs/roadmap.md), and [API research](docs/api-research.md) for scope, implementation,
open verification and dated evidence.

## Local data

Credentials use encrypted per-user storage. Downloads and exports are written beneath the connector's
local data directory and are removed after seven days during a later file operation. Set
`CONFLUENCE_CONNECTOR_HOME` to override the data root.
