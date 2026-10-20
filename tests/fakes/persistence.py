"""In-memory encrypted-persistence fake for local token-store tests."""

from __future__ import annotations

from msal_extensions.persistence import PersistenceNotFound


class FakeEncryptedPersistence:
    is_encrypted = True

    def __init__(self) -> None:
        self.value: str | None = None
        self.save_error: Exception | None = None

    def load(self) -> str:
        if self.value is None:
            raise PersistenceNotFound()
        return self.value

    def save(self, content: str) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.value = content
