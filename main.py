"""Prompt for Wiki credentials and search for SEARCH_TERM. Run: uv run main.py."""

from getpass import getpass
from urllib.parse import urljoin

from confluence_connector.client import ConfluenceClient

WIKI_URL = "https://wiki.example.com"
SEARCH_TERM = "search example codex claude 123"


def main() -> None:
    print(f"Wiki: {WIKI_URL}")
    username = input("Username: ").strip()
    password = getpass("Password (hidden): ")
    if not username or not password:
        print("Enter both a username and password.")
        return

    try:
        with ConfluenceClient(
            WIKI_URL, username=username, password=password
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
            print(urljoin(WIKI_URL + "/", page["url"]))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
