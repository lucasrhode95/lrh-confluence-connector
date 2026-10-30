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

from confluence_connector.config import (
    _read_config,
    data_dir,
    load_config,
    write_cql_timezone_if_missing,
)
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

    def test_selected_config_path_is_cached_and_shared(self) -> None:
        original = self.configure(settings())
        self.config_path.write_text("{}", encoding="utf-8")
        self.assertIs(load_config(self.config_path), original)
        self.assertIs(load_config(), original)
        self.assertIs(load_config(self.config_path.parent / "." / self.config_path.name), original)

    def test_missing_probe_endpoint_has_no_fallback(self) -> None:
        config = settings()
        del config["endpoints"]["spaces"]  # type: ignore[index]
        with self.assertRaisesRegex(SystemExit, "endpoints must define"):
            self.configure(config)

    def test_probe_only_settings_are_optional_but_validated_when_present(self) -> None:
        config = settings()
        for name in ("query", "form_auth_path"):
            del config[name]
        del config["endpoints"]["search_pages"]  # type: ignore[index]
        self.assertNotIn("query", self.configure(config))
        for name, value in (("query", " "), ("form_auth_path", "/absolute")):
            with self.subTest(name=name):
                _read_config.cache_clear()
                invalid = settings()
                invalid[name] = value
                with self.assertRaises(SystemExit):
                    self.configure(invalid)

    def test_cql_timezone_accepts_iana_and_fixed_offsets(self) -> None:
        for timezone_name in ("Atlantic/Reykjavik", "UTC+05:30", "UTC-03:00"):
            with self.subTest(timezone=timezone_name):
                _read_config.cache_clear()
                config = settings()
                config["cql_timezone"] = timezone_name
                self.assertEqual(self.configure(config)["cql_timezone"], timezone_name)

    def test_search_response_budget_is_optional_and_bounded(self) -> None:
        config = settings()
        self.assertNotIn("search_response_max_chars", self.configure(config))
        for budget in (1_000, 40_000, 1_000_000):
            with self.subTest(budget=budget):
                _read_config.cache_clear()
                configured = settings()
                configured["search_response_max_chars"] = budget
                self.assertEqual(self.configure(configured)["search_response_max_chars"], budget)
        for budget in (True, 999, 1_000_001, "40000"):
            with self.subTest(invalid_budget=budget):
                _read_config.cache_clear()
                configured = settings()
                configured["search_response_max_chars"] = budget
                with self.assertRaisesRegex(SystemExit, "search_response_max_chars"):
                    self.configure(configured)

    def test_acceptance_spaces_are_optional_and_allow_personal_space_prefix(self) -> None:
        config = settings()
        config["acceptance"] = {
            "personal_space_key": "~sample.user@example.test",
            "cross_space_key": "SYNTH_TARGET",
            "cross_space_parent_id": "4200",
        }

        loaded = self.configure(config)

        self.assertEqual(loaded["acceptance"], config["acceptance"])

    def test_invalid_acceptance_settings_are_rejected(self) -> None:
        for acceptance in (
            {"personal_space_key": "BAD/KEY"},
            {"cross_space_key": "~"},
            {"cross_space_parent_id": "not-a-page"},
            {"cross_space_parent_id": 4200},
            {"unknown": "SYNTH"},
        ):
            with self.subTest(acceptance=acceptance):
                _read_config.cache_clear()
                config = settings()
                config["acceptance"] = acceptance
                with self.assertRaisesRegex(SystemExit, "acceptance"):
                    self.configure(config)

    def test_invalid_cql_timezone_is_rejected(self) -> None:
        for timezone_name in ("Mars/Olympus", "UTC+15:00", "UTC+00:75", 330):
            with self.subTest(timezone=timezone_name):
                _read_config.cache_clear()
                config = settings()
                config["cql_timezone"] = timezone_name
                with self.assertRaisesRegex(SystemExit, "cql_timezone"):
                    self.configure(config)

    def test_calibration_writes_timezone_atomically_once_and_preserves_other_keys(self) -> None:
        original = settings()
        original["acceptance"] = {
            "personal_space_key": "~sample.user@example.test",
            "cross_space_key": "SYNTH_TARGET",
        }
        self.configure(original)
        with (
            patch.dict(os.environ, {"CONFLUENCE_CONNECTOR_HOME": str(self.folder / "state")}),
            patch("confluence_connector.config.os.replace", wraps=os.replace) as replace,
        ):
            self.assertTrue(write_cql_timezone_if_missing("UTC+05:30"))
            self.assertFalse(write_cql_timezone_if_missing("UTC-03:00"))
        replace.assert_called_once()
        temporary, target = (Path(value) for value in replace.call_args.args)
        self.assertEqual(temporary.parent, target.parent)
        self.assertEqual(target, self.config_path.resolve())
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["cql_timezone"], "UTC+05:30")
        for key, value in original.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(load_config()["cql_timezone"], "UTC+05:30")
        self.assertEqual(list(self.folder.glob(".confluence_config.json.*.tmp")), [])

    def test_config_override_works_before_or_after_a_command(self) -> None:
        parser = build_parser()
        cases = (
            ["--config", str(self.config_path), "status"],
            ["status", "--config", str(self.config_path)],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(parser.parse_args(arguments).config, self.config_path)

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
