"""Prompt for Wiki credentials and search for SEARCH_TERM. Run: uv run main.py."""

from argparse import ArgumentParser
from getpass import getpass
from urllib.parse import urljoin

from confluence_connector.client import ConfluenceClient
from confluence_connector.config import add_config_argument, load_config

SEARCH_TERM = "search example codex claude 123"


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    add_config_argument(parser)
    args = parser.parse_args()
    settings = load_config(args.config)
    wiki_url = settings["base_url"]
    print(f"Wiki: {wiki_url}")
    username = input("Username: ").strip()
    password = getpass("Password (hidden): ")
    if not username or not password:
        print("Enter both a username and password.")
        return

    try:
        with ConfluenceClient(
            settings, username=username, password=password
        ) as wiki:
            print(f"Searching for: {SEARCH_TERM}")
            response = wiki.search_pages(SEARCH_TERM, limit=10)
    except (ValueError, RuntimeError) as exc:
        print(f"Search failed: {exc}")
        print("This example uses HTTP Basic authentication. The Wiki must allow it.")
        return

    results = response["results"]
    if not results:
        print("No matching pages.")
        return

    for page in results:
        print(f"\n{page['title']} (space: {page['space_key']})")
        if page["url"]:
            print(urljoin(wiki_url.rstrip("/") + "/", page["url"]))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
