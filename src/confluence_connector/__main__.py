"""CLI for encrypted Confluence authentication, status and stdio MCP."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from confluence_connector import config
from confluence_connector.auth.store import TokenStore
from confluence_connector.bootstrap import AppContext
from confluence_connector.config import add_config_argument, load_config
from confluence_connector.domain.errors import AuthenticationRequired, ConnectorError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH_REQUIRED = 2
EXIT_INTERRUPTED = 130


def main(argv: Sequence[str] | None = None) -> int:
    """Validate command configuration before prompting or opening the MCP server."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "auth":
            return _auth(args.sign_out, args.config)
        settings = load_config(args.config)
        if args.command == "status":
            return _status(settings, args.check)
        if args.command == "mcp":
            from confluence_connector.logs import configure_app_logging
            from confluence_connector.surfaces.mcp_main import serve_mcp

            configure_app_logging("mcp")
            serve_mcp(context=AppContext(settings))
            return EXIT_OK
        raise AssertionError(f"Unknown command: {args.command}")
    except AuthenticationRequired as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AUTH_REQUIRED
    except ConnectorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


def build_parser() -> argparse.ArgumentParser:
    """Build the documented command tree and preserve the shared private-config override."""
    parser = argparse.ArgumentParser(prog=config.CLI_NAME, description="Confluence Data Center connector.")
    add_config_argument(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    auth = commands.add_parser("auth", help="Store or clear the encrypted Confluence personal access token.")
    auth.add_argument("--sign-out", action="store_true", help="Clear the local token without revoking it.")
    _add_command_config(auth)

    status = commands.add_parser("status", help="Show the local token status.")
    status.add_argument("--check", action="store_true", help="Verify the token with Confluence.")
    _add_command_config(status)

    mcp = commands.add_parser("mcp", help="Run the stdio MCP server.")
    _add_command_config(mcp)
    return parser


def _auth(sign_out: bool, config_path: Path | None) -> int:
    store = TokenStore()
    if sign_out:
        print("This clears the local token. It cannot be recovered and is not revoked on the Confluence server.")
        existed = store.sign_out()
        print("Local Confluence token cleared." if existed else "No local Confluence token was stored.")
        return EXIT_OK

    settings = load_config(config_path)
    token = getpass.getpass("Confluence personal access token: ").strip()
    if not token:
        raise AuthenticationRequired("The Confluence token cannot be empty.")

    async def verify_and_store() -> str:
        context = AppContext(settings, store=store)
        try:
            user = await context.verify_token(token)
            store.save(token=token, username=user.username)
            return user.username
        finally:
            await context.aclose()

    username = asyncio.run(verify_and_store())
    print(f"Confluence token stored securely for {username}.")
    return EXIT_OK


def _status(settings: dict[str, Any], check: bool) -> int:
    async def report() -> tuple[bool, str | None]:
        context = AppContext(settings)
        try:
            status = await (context.check_auth_status() if check else context.auth_status())
            return status.token_stored, status.username
        finally:
            await context.aclose()

    stored, username = asyncio.run(report())
    print(f"Confluence instance: {settings['base_url']}")
    if not stored:
        print("No Confluence token is stored.")
        return EXIT_AUTH_REQUIRED if check else EXIT_OK
    print(f"Token stored for: {username}")
    print("Token verified with Confluence." if check else "Offline status only; use --check to verify.")
    return EXIT_OK


def _add_command_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="Override CONFLUENCE_CONFIG or the repository's confluence_config.json.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
