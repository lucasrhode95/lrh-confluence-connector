"""Offline checks for secret redaction in application logs."""

from __future__ import annotations

import logging
import unittest

from confluence_connector.logs import SecretRedactionFilter


class LoggingChecks(unittest.TestCase):
    def test_redaction_removes_synthetic_credentials_from_messages(self) -> None:
        record = logging.LogRecord(
            "synthetic",
            logging.ERROR,
            "test_logs.py",
            1,
            "Authorization: Bearer pat-value access_token=secret-value "
            "https://example.test/callback?code=query-value&state=state-value",
            (),
            None,
        )

        self.assertTrue(SecretRedactionFilter().filter(record))
        message = record.getMessage()
        for secret in ("pat-value", "secret-value", "query-value", "state-value"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, message)


if __name__ == "__main__":
    unittest.main()
