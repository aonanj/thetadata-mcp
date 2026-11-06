"""Exercise authenticated HTTP, MCP dispatch and hosted configuration without provider calls."""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from starlette.testclient import TestClient
from http_app import create_app, transport_security

TOKEN = "test-service-token-" + "a" * 32
PROVIDER_KEY = "fake-provider-key-for-tests-only"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {"MCP_API_KEY": TOKEN, "THETADATA_API_KEY": PROVIDER_KEY,
                                          "MCP_ALLOWED_HOSTS": "testserver", "RENDER_EXTERNAL_HOSTNAME": ""})
        cls.env.start()
        cls.client = TestClient(create_app())
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        cls.env.stop()

    def rpc(self, method, params=None, headers=None):
        return self.client.post("/mcp", headers=headers or HEADERS,
                                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_health_is_public_and_contains_no_secrets(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn(PROVIDER_KEY, response.text)
        self.assertEqual(self.client.head("/health").status_code, 200)
        self.assertEqual(self.client.post("/health").status_code, 401)

    def test_unauthenticated_wrong_or_query_token_are_rejected(self):
        for path, headers in (("/mcp", {}), ("/mcp/", {}), ("/mcp", {"Authorization": "Bearer wrong"}),
                              (f"/mcp?api_key={TOKEN}", {})):
            response = self.client.post(path, headers=headers, json={})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
            self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_duplicate_authorization_headers_are_rejected(self):
        response = self.client.post("/mcp", headers=[("Authorization", f"Bearer {TOKEN}"),
                                                    ("Authorization", f"Bearer {TOKEN}")], json={})
        self.assertEqual(response.status_code, 401)

    def test_mcp_initialize_and_tool_discovery(self):
        initialized = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "http-test", "version": "1.0"}})
        self.assertEqual(initialized.status_code, 200)
        self.assertIn("protocolVersion", initialized.json()["result"])
        response = self.rpc("tools/list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({t["name"] for t in response.json()["result"]["tools"]},
                         {"get_status", "list_expirations", "get_option_chain", "get_option_contract"})
        self.assertNotIn("mcp-session-id", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_http_tool_call_and_validation(self):
        response = self.rpc("tools/call", {"name": "get_status", "arguments": {}})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(TOKEN, response.text)
        self.assertNotIn(PROVIDER_KEY, response.text)
        self.assertIn("streamable-http", response.text)
        response = self.rpc("tools/call", {"name": "get_option_chain", "arguments": {"symbol": "QQQ", "max_dte": -1}})
        self.assertTrue(response.json()["result"]["isError"])

    def test_http_dispatches_validated_provider_arguments(self):
        with patch("main.execute", new_callable=AsyncMock, return_value={"symbol": "QQQ", "expirations": []}) as execute:
            response = self.rpc("tools/call", {"name": "list_expirations", "arguments": {"symbol": " qqq "}})
            self.assertEqual(response.status_code, 200)
            execute.assert_awaited_once_with("expirations", {"symbol": "QQQ", "start": None, "end": None})

    def test_untrusted_host_or_origin_is_rejected(self):
        for override in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"}):
            response = self.rpc("tools/list", headers={**HEADERS, **override})
            self.assertIn(response.status_code, (403, 421))

    def test_missing_or_weak_hosted_secrets_fail_startup(self):
        for token in ("", "short", "a" * 31 + " "):
            with patch.dict(os.environ, {"MCP_API_KEY": token}):
                with self.assertRaises(ValueError):
                    create_app()
        with patch.dict(os.environ, {"THETADATA_API_KEY": ""}):
            with self.assertRaises(ValueError):
                create_app()

    def test_render_and_custom_hosts_are_allowed_explicitly(self):
        with patch.dict(os.environ, {"RENDER_EXTERNAL_HOSTNAME": "thetadata-test.onrender.com",
                                     "MCP_ALLOWED_HOSTS": "options.example.com"}):
            settings = transport_security()
        self.assertTrue(settings.enable_dns_rebinding_protection)
        self.assertIn("thetadata-test.onrender.com", settings.allowed_hosts)
        self.assertIn("options.example.com", settings.allowed_hosts)
        with patch.dict(os.environ, {"MCP_ALLOWED_HOSTS": "https://bad.example/path"}):
            with self.assertRaises(ValueError):
                transport_security()


if __name__ == "__main__":
    unittest.main()
