"""Synthetic offline checks. No real capture, account or network is used."""

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
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "probes"))
sys.path.insert(0, str(ROOT / "src"))

from extract_inventory import Inventory, normalize_route
from confluence_connector.config import (
    _read_config,
    add_config_argument,
    capture_inputs,
    load_config,
)
import probe_auth


def settings():
    return {
        "base_url": "https://private.example.test/wiki",
        "query": "synthetic search",
        "form_auth_path": "rest/tsv/1.0/authenticate",
        "captures": [],
        "endpoints": {
            "current_user": "rest/api/user/current",
            "search_pages": "rest/api/content/search",
            "ui_search": "rest/api/search",
            "spaces": "rest/api/space",
            "content": "rest/api/content/{page_id}",
        },
    }


class ResearchChecks(unittest.TestCase):
    def setUp(self):
        _read_config.cache_clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config_path = self.folder / "confluence_config.json"

    def configure(self, config):
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        return load_config(self.config_path)

    def test_configuration_is_required(self):
        with self.assertRaisesRegex(SystemExit, "Missing Confluence configuration"):
            load_config(self.config_path)

    def test_missing_setting_has_no_fallback(self):
        config = settings()
        del config["base_url"]
        with self.assertRaises(SystemExit):
            self.configure(config)

    def test_relative_auth_path_preserves_instance_context(self):
        config = self.configure(settings())
        with probe_auth._new_client(config["base_url"]) as client:
            request = client.build_request("POST", config["form_auth_path"])
            self.assertEqual(
                str(request.url), "https://private.example.test/wiki/rest/tsv/1.0/authenticate"
            )

    def test_capture_alias_must_be_neutral(self):
        config = settings()
        config["captures"] = [{"file": "input.har", "label": "PrivateCompany"}]
        with self.assertRaisesRegex(SystemExit, "neutral aliases"):
            self.configure(config)

    def test_capture_aliases_cannot_collide(self):
        config = settings()
        config["captures"] = [
            {"file": "one.har", "label": "HAR01"},
            {"file": "two.har", "label": "HAR01"},
        ]
        with self.assertRaisesRegex(SystemExit, "unique"):
            self.configure(config)

    def test_capture_paths_cannot_escape_input_folder(self):
        config = settings()
        config["captures"] = [{"file": "../input.har", "label": "HAR01"}]
        with self.assertRaisesRegex(SystemExit, "basename"):
            self.configure(config)

    def test_capture_list_can_be_empty_for_live_auth_only(self):
        config = self.configure(settings())
        with self.assertRaisesRegex(SystemExit, "at least one capture"):
            capture_inputs(config, self.folder)

    def test_unsafe_target_url_is_rejected(self):
        for url in (
            "http://private.example.test",
            "https://user:secret@private.example.test",
            "https://private.example.test?token=secret",
        ):
            with self.subTest(url=url):
                config = settings()
                config["base_url"] = url
                with self.assertRaises(SystemExit):
                    self.configure(config)

    def test_local_host_and_page_identifiers_are_anonymized(self):
        host, route, category, keys = normalize_route(
            "https://private.example.test/display/PrivateProject/PrivatePage?token=secret",
            "private.example.test",
            "",
        )
        self.assertEqual(
            (host, route, category),
            ("confluence.example.com", "/display/{space}/{title}", "content-link"),
        )
        self.assertEqual(keys, {"token"})

    def test_microsoft_sso_tenant_domain_is_anonymized(self):
        host, route, category, keys = normalize_route(
            "https://autologon.microsoftazuread-sso.com/company.example.test/winauth/ssoprobe",
            "private.example.test",
            "",
        )
        self.assertEqual(route, "/{tenant-domain}/winauth/ssoprobe")

    def test_har_alias_replaces_filename_and_provenance_uses_real_entry(self):
        path = self.folder / "private-source.har"
        path.write_text(
            json.dumps(
                {
                    "log": {
                        "entries": [
                            {
                                "request": {
                                    "url": "https://private.example.test/rest/api/user/current",
                                    "method": "GET",
                                },
                                "response": {
                                    "status": 200,
                                    "content": {
                                        "mimeType": "application/javascript",
                                        "text": 'const base="/rest/tsv/1.0";const suffix="/authenticate";',
                                    },
                                },
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        inventory = Inventory("private.example.test", "")
        inventory.read_har(path, "HAR01")
        out = self.folder / "output"
        out.mkdir()
        inventory.write(out)
        published = "".join(p.read_text() for p in out.iterdir())
        self.assertNotIn(path.name, published)
        self.assertNotIn("private.example.test", published)
        source_ids = {row["id"] for row in inventory.sources}
        for item in inventory.records.values():
            for source in item["sources"]:
                self.assertIn(source.removesuffix(":body"), source_ids)
        self.assertEqual(inventory.captures[0]["name"], "HAR01")

    def test_zip_member_names_and_local_host_are_not_published(self):
        path = self.folder / "private-archive.zip"
        with ZipFile(path, "w") as archive:
            archive.writestr(
                "private.example.test/display/PrivateProject/PrivatePage.html",
                '<html><a href="/display/PrivateProject/PrivatePage">Private Person</a></html>',
            )
        inventory = Inventory("private.example.test", "")
        inventory.read_zip(path, "ZIP01")
        out = self.folder / "output"
        out.mkdir()
        inventory.write(out)
        published = "".join(p.read_text() for p in out.iterdir())
        for private in (
            "private.example.test",
            path.name,
            "PrivateProject",
            "PrivatePage",
            "Private Person",
        ):
            self.assertNotIn(private, published)
        self.assertEqual(inventory.sources[0]["id"], "ZIP01#001")

    def test_missing_configuration_fails_before_credentials_or_network(self):
        args = argparse.Namespace(config=self.config_path, base_url=None, query=None, mode="pat")
        with patch.object(probe_auth, "_parse_args", return_value=args):
            with patch.object(probe_auth.getpass, "getpass") as prompt:
                with patch.object(probe_auth, "_new_client") as client:
                    with self.assertRaises(SystemExit):
                        probe_auth.main()
                    prompt.assert_not_called()
                    client.assert_not_called()

    def test_configuration_is_loaded_once_per_process(self):
        original = self.configure(settings())
        self.config_path.write_text("{}", encoding="utf-8")
        self.assertIs(load_config(self.config_path), original)

    def test_missing_app_endpoint_has_no_fallback(self):
        config = settings()
        del config["endpoints"]["spaces"]
        with self.assertRaisesRegex(SystemExit, "endpoints must define"):
            self.configure(config)

    def test_app_uses_configured_paths_and_context(self):
        import httpx
        from confluence_connector.client import ConfluenceClient

        config = settings()
        config["endpoints"]["search_pages"] = "custom/search"
        config["endpoints"]["spaces"] = "custom/spaces"
        config["endpoints"]["content"] = "custom/content/{page_id}"
        config = self.configure(config)
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"results": [], "id": "123"})

        http_client = httpx.Client

        def fake_client(**kwargs):
            return http_client(**kwargs, transport=httpx.MockTransport(respond))

        with patch("confluence_connector.client.httpx.Client", side_effect=fake_client):
            with ConfluenceClient(config, pat="synthetic-token") as client:
                client.search_pages("synthetic")
                client.list_spaces()
                client.get_page("123")
        self.assertEqual(
            [request.url.path for request in requests],
            ["/wiki/custom/search", "/wiki/custom/spaces", "/wiki/custom/content/123"],
        )
        self.assertTrue(
            all(
                request.headers["Authorization"] == "Bearer synthetic-token" for request in requests
            )
        )

    def test_demo_requires_configuration_before_prompting(self):
        import main as demo

        with (
            patch("sys.argv", ["main.py"]),
            patch.object(demo, "load_config", side_effect=SystemExit("Missing")),
        ):
            with patch("builtins.input") as username:
                with patch.object(demo, "getpass") as password:
                    with self.assertRaises(SystemExit):
                        demo.main()
                    username.assert_not_called()
                    password.assert_not_called()

    def test_context_path_does_not_leak_private_page_names(self):
        host, route, category, keys = normalize_route(
            "https://private.example.test/private-install/display/PrivateProject/PrivatePage",
            "private.example.test",
            "/private-install",
        )
        self.assertEqual((host, route), ("confluence.example.com", "/display/{space}/{title}"))

    def test_relative_and_absolute_paths_share_the_same_settings(self):
        original = self.configure(settings())
        self.config_path.write_text("{}", encoding="utf-8")
        self.assertIs(load_config(self.config_path.parent / "." / self.config_path.name), original)

    def test_command_override_is_shared_with_later_callers(self):
        expected = self.configure(settings())
        self.assertIs(load_config(), expected)

    def test_every_command_uses_the_same_optional_flag(self):
        parser = argparse.ArgumentParser()
        add_config_argument(parser)
        self.assertIsNone(parser.parse_args([]).config)
        self.assertEqual(
            parser.parse_args(["--config", str(self.config_path)]).config, self.config_path
        )

    def test_server_uses_selected_override_for_tool_clients(self):
        from confluence_connector import server

        expected = self.configure(settings())
        with patch("sys.argv", ["confluence-connector", "--config", str(self.config_path)]):
            with patch.object(server.mcp, "run") as run:
                server.main()
                run.assert_called_once_with("stdio")
        self.assertIs(load_config(), expected)

    def test_environment_override_works_outside_repository(self):
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
        self.assertEqual(result.stdout.strip(), "https://private.example.test/wiki")

    def test_default_file_location_is_independent_of_working_directory(self):
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


if __name__ == "__main__":
    unittest.main()
