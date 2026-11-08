import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

source = Path(__file__).resolve().parents[1] / "scripts/package_remote.py"
spec = importlib.util.spec_from_file_location("package_remote", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PackagingTests(unittest.TestCase):
    def test_remote_package_contains_connection_without_embedded_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = module.package("https://options.example.com/mcp", Path(directory) / "remote.zip")
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {"plugin.json", "mcp.json"})
                manifest = json.loads(archive.read("plugin.json"))
                server = json.loads(archive.read("mcp.json"))["mcpServers"]["thetadata-options"]
        self.assertFalse(manifest["extensions"]["com.openai"]["requires_local_executor"])
        self.assertEqual(server["type"], "streamable-http")
        self.assertEqual(server["extensions"]["com.openai"]["auth"],
                         {"type": "oauth", "baseScopes": ["options:read"], "client": {"mode": "cimd"}})
        self.assertNotIn("headers", server)

    def test_explicit_client_registration_and_legacy_bearer(self):
        configurations = [
            ({"auth": "bearer"}, {"type": "api_key", "headerScheme": "bearer"}),
            ({"client_mode": "provided", "client_id": "public-client-id", "token_endpoint_auth_method": "client_secret_post"},
             {"type": "oauth", "baseScopes": ["options:read"],
              "client": {"mode": "provided", "clientId": "public-client-id", "tokenEndpointAuthMethod": "client_secret_post"}}),
            ({"client_mode": "cimd"}, {"type": "oauth", "baseScopes": ["options:read"], "client": {"mode": "cimd"}}),
            ({"client_mode": "dcr"}, {"type": "oauth", "baseScopes": ["options:read"], "client": {"mode": "dcr"}}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for arguments, expected in configurations:
                path = module.package("https://options.example.com/mcp", Path(directory) / "remote.zip", **arguments)
                with zipfile.ZipFile(path) as archive:
                    config = json.loads(archive.read("mcp.json"))
                    self.assertEqual(config["mcpServers"]["thetadata-options"]["extensions"]["com.openai"]["auth"], expected)
                    self.assertNotIn("clientSecret", archive.read("mcp.json").decode())

    def test_incomplete_or_contradictory_auth_settings_are_rejected(self):
        for arguments in ({"auth": "none"}, {"client_mode": "provided"}, {"client_id": "id"},
                          {"client_mode": "provided", "client_id": "id"},
                          {"auth": "bearer", "client_mode": "dcr"}, {"client_mode": "cimd", "client_id": "id"},
                          {"client_mode": "dcr", "client_id": "id"},
                          {"client_mode": "cimd", "token_endpoint_auth_method": "client_secret_post"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                module.package("https://options.example.com/mcp", Path("unused.zip"), **arguments)

    def test_remote_package_rejects_insecure_or_secret_bearing_urls(self):
        for url in ("http://options.example.com/mcp", "https://user:secret@options.example.com/mcp",
                    "https://options.example.com/mcp?key=secret", "https://options.example.com/not-mcp"):
            with self.assertRaises(ValueError):
                module.package(url, Path("unused.zip"))


if __name__ == "__main__":
    unittest.main()
