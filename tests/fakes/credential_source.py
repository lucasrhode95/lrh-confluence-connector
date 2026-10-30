"""Fake current-credential source with no filesystem or keychain access."""

from __future__ import annotations

from confluence_connector.auth.store import StoredCredential


class FakeCredentialSource:
    def __init__(self, username: str = "sample-user", token: str = "synthetic-pat") -> None:
        self.credential: StoredCredential | None = StoredCredential(username=username, token=token)
        self.loads = 0

    def load(self) -> StoredCredential | None:
        self.loads += 1
        return self.credential

    def revision(self) -> object | None:
        return None if self.credential is None else (self.credential.username, self.credential.token)
