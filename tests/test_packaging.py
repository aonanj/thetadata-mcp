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
        self.assertEqual(server["extensions"]["com.openai"]["auth"], {"type": "api_key", "headerScheme": "bearer"})
        self.assertNotIn("headers", server)

    def test_remote_package_rejects_insecure_or_secret_bearing_urls(self):
        for url in ("http://options.example.com/mcp", "https://user:secret@options.example.com/mcp",
                    "https://options.example.com/mcp?key=secret", "https://options.example.com/not-mcp"):
            with self.assertRaises(ValueError):
                module.package(url, Path("unused.zip"))


if __name__ == "__main__":
    unittest.main()
