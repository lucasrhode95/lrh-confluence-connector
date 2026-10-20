"""Errors translated at the CLI and MCP boundaries."""

from __future__ import annotations


class ConnectorError(Exception):
    """Base for expected connector failures that are safe to show to the caller."""


class AuthenticationRequired(ConnectorError):
    """No accepted Confluence token is stored, or the instance rejected it."""

    def __init__(self, message: str, *, command: str = "confluence-connector auth") -> None:
        self.command = command
        super().__init__(f"{message} Run `{command}` to authenticate.")


class AccountMismatch(ConnectorError):
    """The supplied or stored token belongs to a different Confluence user."""


class SecureStorageUnavailable(ConnectorError):
    """Encrypted token persistence could not be used safely."""


class InvalidRequest(ConnectorError):
    """An input does not satisfy a public service contract."""


class NotFound(ConnectorError):
    """A selected Confluence resource was not found or is not readable."""


class Throttled(ConnectorError):
    """Confluence is throttling the request."""


class Upstream(ConnectorError):
    """Confluence could not be reached or returned an unexpected response."""


class PermissionDenied(Upstream):
    """Confluence denied this operation for the current account or resource."""


class Conflict(Upstream):
    """Confluence rejected an operation because the resource changed concurrently."""


class UncertainWrite(Upstream):
    """A write may have reached Confluence, so it was not sent again."""
