"""Offline checks for collision-safe and private local output paths."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from confluence_connector.service import localfiles


class LocalFileChecks(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are unavailable")
    def test_output_directories_and_claimed_files_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch(
            "confluence_connector.config.data_dir", return_value=Path(temporary)
        ):
            output = localfiles.kept_dir("exports")
            account = localfiles.private_dir(output / "synthetic-account")
            claimed = localfiles.claim(account, "export.zip")

            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(account.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(claimed.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
