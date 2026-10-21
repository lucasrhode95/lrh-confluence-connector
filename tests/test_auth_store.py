"""Encrypted token-store behavior using a synthetic persistence and lock."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from confluence_connector.auth.store import TokenStore
from confluence_connector.domain.errors import AccountMismatch, SecureStorageUnavailable
from tests.fakes.persistence import FakeEncryptedPersistence


class RecordingLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self.entered = False

    def __enter__(self) -> RecordingLock:
        self.entered = True
        return self

    def __exit__(self, *_: object) -> None:
        return None


class TokenStoreChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "token-cache.bin"
        self.persistence = FakeEncryptedPersistence()
        self.locks: list[RecordingLock] = []
        self.persistence_patch = patch(
            "confluence_connector.auth.store.build_encrypted_persistence",
            return_value=self.persistence,
        )
        self.persistence_patch.start()
        self.addCleanup(self.persistence_patch.stop)

        def new_lock(path: str) -> RecordingLock:
            lock = RecordingLock(path)
            self.locks.append(lock)
            return lock

        self.lock_patch = patch("confluence_connector.auth.store.CrossPlatLock", side_effect=new_lock)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.store = TokenStore(self.path, base_url="https://confluence.example.test/wiki")

    def test_absent_store_is_signed_out_and_reads_are_locked(self) -> None:
        self.assertIsNone(self.store.load())
        self.assertTrue(self.locks[-1].entered)
        self.assertEqual(self.locks[-1].path, f"{self.path}.acquire.lock")

    def test_credential_and_username_are_written_as_one_encrypted_record(self) -> None:
        self.store.save(token="synthetic-pat", username="sample-user")
        credential = self.store.load()
        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(credential.username, "sample-user")
        self.assertEqual(credential.token, "synthetic-pat")
        self.assertEqual(json.loads(self.persistence.value)["base_url"], "https://confluence.example.test/wiki")
        self.assertTrue(all(lock.entered for lock in self.locks))

    def test_instance_switch_fails_closed_but_sign_out_can_clear_the_record(self) -> None:
        self.store.save(token="synthetic-pat", username="sample-user")
        for base_url in ("https://other.example.test/wiki", "https://confluence.example.test/other"):
            with self.subTest(base_url=base_url):
                other = TokenStore(self.path, base_url=base_url)
                with self.assertRaisesRegex(AccountMismatch, "different Confluence instance"):
                    other.load()
                with self.assertRaises(AccountMismatch):
                    other.save(token="another-token", username="sample-user")
        self.assertTrue(TokenStore(self.path).sign_out())

    def test_unbound_record_is_rejected_without_migration(self) -> None:
        self.persistence.value = '{"username":"sample-user","token":"synthetic-pat"}'
        with self.assertRaisesRegex(SecureStorageUnavailable, "unbound"):
            self.store.load()

    def test_canonical_instance_url_accepts_default_port_and_trailing_slash(self) -> None:
        self.store.save(token="synthetic-pat", username="sample-user")
        same = TokenStore(self.path, base_url="https://CONFLUENCE.example.test:443/wiki/")
        self.assertIsNotNone(same.load())

    def test_account_switch_requires_explicit_sign_out(self) -> None:
        self.store.save(token="synthetic-pat-a", username="sample-user-a")
        with self.assertRaisesRegex(AccountMismatch, "auth --sign-out"):
            self.store.save(token="synthetic-pat-b", username="sample-user-b")
        self.assertEqual(self.store.load().username, "sample-user-a")  # type: ignore[union-attr]

    def test_sign_out_clears_the_token_for_all_storage_backends(self) -> None:
        self.store.save(token="synthetic-pat", username="sample-user")
        self.assertTrue(self.store.sign_out())
        self.assertIsNone(self.store.load())
        self.assertEqual(self.persistence.value, "")

    def test_sign_out_clears_malformed_encrypted_record_under_lock(self) -> None:
        self.persistence.value = "{broken"
        with self.assertRaises(SecureStorageUnavailable):
            self.store.load()
        self.assertTrue(self.store.sign_out())
        self.assertIsNone(self.store.load())
        self.assertTrue(self.locks[-1].entered)

    def test_sign_out_when_already_empty_is_safe(self) -> None:
        self.assertFalse(self.store.sign_out())
        self.assertIsNone(self.store.load())

    def test_plaintext_persistence_is_rejected(self) -> None:
        self.persistence.is_encrypted = False
        with self.assertRaisesRegex(SecureStorageUnavailable, "No plaintext fallback"):
            self.store.load()

    def test_missing_encryption_backend_fails_closed(self) -> None:
        with patch(
            "confluence_connector.auth.store.build_encrypted_persistence",
            side_effect=OSError("synthetic backend failure"),
        ):
            with self.assertRaisesRegex(SecureStorageUnavailable, "No plaintext fallback"):
                self.store.load()

    def test_malformed_record_fails_closed(self) -> None:
        self.persistence.value = '{"username": "sample-user", "token": "synthetic-pat", "extra": true}'
        with self.assertRaisesRegex(SecureStorageUnavailable, "malformed"):
            self.store.load()

    def test_storage_error_does_not_echo_secret_text(self) -> None:
        self.persistence.save_error = OSError("synthetic-pat")
        with self.assertRaises(SecureStorageUnavailable) as caught:
            self.store.save(token="synthetic-pat", username="sample-user")
        self.assertNotIn("synthetic-pat", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
