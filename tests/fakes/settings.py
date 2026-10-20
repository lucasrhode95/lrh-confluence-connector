"""Synthetic deployment settings for offline connector tests."""

from __future__ import annotations


def settings() -> dict[str, object]:
    return {
        "base_url": "https://confluence.example.test/wiki",
        "query": "synthetic search",
        "form_auth_path": "rest/tsv/1.0/authenticate",
        "endpoints": {
            "current_user": "rest/api/user/current",
            "search_pages": "rest/api/content/search",
            "ui_search": "rest/api/search",
            "spaces": "rest/api/space",
            "content": "rest/api/content/{page_id}",
        },
    }
