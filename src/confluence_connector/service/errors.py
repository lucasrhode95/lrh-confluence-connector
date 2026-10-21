"""Shared error handling for account-bound service operations."""

from __future__ import annotations

from confluence_connector.domain.errors import AccountMismatch, AuthenticationRequired, ConnectorError


def raise_account_error(error: ConnectorError) -> None:
    """Reraise account-boundary failures so callers cannot downgrade them to item failures."""
    if isinstance(error, (AccountMismatch, AuthenticationRequired)):
        raise error
