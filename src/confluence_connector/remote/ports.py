"""Ports between Confluence adapters and encrypted local authentication state."""

from __future__ import annotations

from typing import Protocol

from confluence_connector.auth.store import StoredCredential


class CredentialSource(Protocol):
    """A source that reads the current account-bound token for each remote request."""

    def load(self) -> StoredCredential | None: ...
