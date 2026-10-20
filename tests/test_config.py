"""Synthetic settings and research-probe configuration checks; no live network is used."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research"))

from confluence_connector.config import _read_config, add_config_argument, data_dir, load_config
from confluence_connector.__main__ import build_parser
import probe_auth

from fakes.settings import settings


class ConfigurationChecks(unittest.TestCase):
    def setUp(self) -> None:
        _read_config.cache_clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config_path = self.folder / "confluence_config.json"

    def configure(self, config: dict[str, object]) -> dict[str, object]:
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        return load_config(self.config_path)

    def test_configuration_is_required(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Missing Confluence configuration"):
            load_config(self.config_path)

    def test_missing_setting_has_no_fallback(self) -> None:
        config = settings()
        del config["base_url"]
        with self.assertRaises(SystemExit):
            self.configure(config)

    def test_relative_auth_path_preserves_instance_context(self) -> None:
        config = self.configure(settings())
        with probe_auth._new_client(str(config["base_url"])) as client:
            request = client.build_request("POST", str(config["form_auth_path"]))
        self.assertEqual(
            str(request.url), "https://confluence.example.test/wiki/rest/tsv/1.0/authenticate"
        )

    def test_unsafe_target_url_is_rejected(self) -> None:
        for url in (
            "http://confluence.example.test",
            "https://user:synthetic@confluence.example.test",
            "https://confluence.example.test?token=synthetic",
            "https://confluence.example.test:invalid/wiki",
        ):
            with self.subTest(url=url):
                config = settings()
                config["base_url"] = url
                with self.assertRaises(SystemExit):
                    self.configure(config)

    def test_missing_configuration_fails_before_probe_prompt_or_network(self) -> None:
        args = argparse.Namespace(config=self.config_path, base_url=None, query=None, mode="pat")
        with patch.object(probe_auth, "_parse_args", return_value=args):
            with patch.object(probe_auth.getpass, "getpass") as prompt:
                with patch.object(probe_auth, "_new_client") as client:
                    with self.assertRaises(SystemExit):
                        probe_auth.main()
                    prompt.assert_not_called()
                    client.assert_not_called()

    def test_configuration_is_loaded_once_per_process(self) -> None:
        original = self.configure(settings())
        self.config_path.write_text("{}", encoding="utf-8")
        self.assertIs(load_config(self.config_path), original)

    def test_missing_probe_endpoint_has_no_fallback(self) -> None:
        config = settings()
        del config["endpoints"]["spaces"]  # type: ignore[index]
        with self.assertRaisesRegex(SystemExit, "endpoints must define"):
            self.configure(config)

    def test_relative_and_absolute_paths_share_the_same_settings(self) -> None:
        original = self.configure(settings())
        self.config_path.write_text("{}", encoding="utf-8")
        self.assertIs(load_config(self.config_path.parent / "." / self.config_path.name), original)

    def test_command_override_is_shared_with_later_callers(self) -> None:
        expected = self.configure(settings())
        self.assertIs(load_config(), expected)

    def test_config_override_works_before_or_after_a_command(self) -> None:
        parser = build_parser()
        before = parser.parse_args(["--config", str(self.config_path), "status"])
        after = parser.parse_args(["status", "--config", str(self.config_path)])
        self.assertEqual(before.config, self.config_path)
        self.assertEqual(after.config, self.config_path)

    def test_environment_override_works_outside_repository(self) -> None:
        self.configure(settings())
        environment = dict(os.environ, CONFLUENCE_CONFIG=str(self.config_path))
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from confluence_connector.config import load_config; print(load_config()['base_url'])",
            ],
            cwd=self.folder,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "https://confluence.example.test/wiki")

    def test_default_file_location_is_independent_of_working_directory(self) -> None:
        environment = dict(os.environ)
        environment.pop("CONFLUENCE_CONFIG", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from confluence_connector.config import CONFIG_PATH; print(CONFIG_PATH)",
            ],
            cwd=self.folder,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(Path(result.stdout.strip()), ROOT / "confluence_config.json")

    def test_data_directory_override_is_resolved(self) -> None:
        with patch.dict(os.environ, {"CONFLUENCE_CONNECTOR_HOME": str(self.folder / "state")}):
            self.assertEqual(data_dir(), (self.folder / "state").resolve())


if __name__ == "__main__":
    unittest.main()
