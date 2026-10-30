"""Encrypted, cross-process-safe storage for one Confluence PAT and its account name."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from typing import Any

from msal_extensions import CrossPlatLock, build_encrypted_persistence
from msal_extensions.persistence import PersistenceNotFound

from confluence_connector import config
from confluence_connector.domain.errors import AccountMismatch, SecureStorageUnavailable


@dataclass(frozen=True)
class StoredCredential:
    username: str
    token: str = field(repr=False)


class TokenStore:
    """Read and replace the encrypted record while holding the shared cache lock.

    Only sign-out can use an unbound store. Reads and saves require the validated configured base URL.
    Public methods accept an already-verified username and nonempty PAT. Internal decoding trusts
    no stored bytes and fails closed when the current encrypted record is malformed.
    """

    def __init__(self, path: Path | None = None, *, base_url: str | None = None) -> None:
        self._base_url = str(httpx.URL(base_url)).rstrip("/") if base_url else None
        self.path = path or config.data_dir() / "token-cache.bin"
        self.lock_path = Path(f"{self.path}.acquire.lock")

    def revision(self) -> object | None:
        """Return the store file's change marker, without decrypting; None when it cannot be read.

        Every save and sign-out rewrites the file (keychain backends touch it as their change signal),
        so an unchanged marker means the stored account is unchanged.
        """
        try:
            status = self.path.stat()
        except OSError:
            return None
        return (status.st_mtime_ns, status.st_size)

    def load(self) -> StoredCredential | None:
        """Return the current credential under the cross-process lock, or ``None`` if signed out."""
        with self._lock():
            return self._read(self._persistence())

    def save(self, *, token: str, username: str) -> None:
        """Persist a verified credential without silently changing the bound Confluence account."""
        if not token or not token.strip() or not username or not username.strip():
            raise ValueError("A nonempty verified token and username are required.")
        if self._base_url is None:
            raise ValueError("A configured instance is required to store a token.")
        credential = StoredCredential(username=username.strip(), token=token.strip())
        with self._lock():
            persistence = self._persistence()
            current = self._read(persistence)
            if current and current.username.casefold() != credential.username.casefold():
                raise AccountMismatch(
                    "The stored token belongs to another Confluence user. Run "
                    "`confluence-connector auth --sign-out` before switching accounts."
                )
            try:
                persistence.save(json.dumps({"username": credential.username, "token": credential.token, "base_url": self._base_url}))
            except Exception as exc:
                raise SecureStorageUnavailable(
                    f"Encrypted token storage could not save the credential ({type(exc).__name__})."
                ) from None

    def sign_out(self) -> bool:
        """Clear the token under lock; an empty encrypted value also clears keychain backends."""
        with self._lock():
            persistence = self._persistence()
            try:
                existed = bool(persistence.load())
            except PersistenceNotFound:
                existed = False
            except Exception as exc:
                raise SecureStorageUnavailable(
                    f"Encrypted token storage could not read the credential ({type(exc).__name__})."
                ) from None
            try:
                persistence.save("")
            except Exception as exc:
                raise SecureStorageUnavailable(
                    f"Encrypted token storage could not clear the credential ({type(exc).__name__})."
                ) from None
            return existed

    def _persistence(self) -> Any:
        try:
            persistence = build_encrypted_persistence(str(self.path))
        except Exception as exc:
            raise SecureStorageUnavailable(
                f"Encrypted token storage is unavailable ({type(exc).__name__}). No plaintext fallback is used."
            ) from None
        if not persistence.is_encrypted:
            raise SecureStorageUnavailable("Encrypted token storage is unavailable. No plaintext fallback is used.")
        return persistence

    def _read(self, persistence: Any) -> StoredCredential | None:
        try:
            raw = persistence.load()
        except PersistenceNotFound:
            return None
        except Exception as exc:
            raise SecureStorageUnavailable(
                f"Encrypted token storage could not read the credential ({type(exc).__name__})."
            ) from None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise SecureStorageUnavailable("The encrypted credential record is malformed or unbound; run `confluence-connector auth --sign-out`, then `auth`.")
        if (
            not isinstance(data, dict)
            or set(data) != {"username", "token", "base_url"}
            or not isinstance(data.get("base_url"), str)
            or not data["base_url"]
            or not isinstance(data.get("username"), str)
            or not data["username"].strip()
            or not isinstance(data.get("token"), str)
            or not data["token"].strip()
        ):
            raise SecureStorageUnavailable("The encrypted credential record is malformed or unbound; run `confluence-connector auth --sign-out`, then `auth`.")
        if self._base_url is None or data["base_url"] != self._base_url:
            raise AccountMismatch(
                "The stored token belongs to a different Confluence instance; run "
                "`confluence-connector auth --sign-out` before signing in to this instance."
            )
        return StoredCredential(username=data["username"].strip(), token=data["token"].strip())

    def _lock(self) -> CrossPlatLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return CrossPlatLock(str(self.lock_path))
