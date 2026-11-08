"""Signed tokens through the HTTP boundary; all issuer/provider IO is controlled."""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import check_remote
from http_app import create_app, mcp
from oauth import METADATA_PATH, OAuthSettings

ISSUER = "https://thetadata-options.us.auth0.com/"
RESOURCE = "https://options.example.com/mcp"
SUBJECT = "auth0|test-owner"
ENV = {"MCP_AUTH_MODE": "oauth", "MCP_OAUTH_ISSUER": ISSUER,
       "MCP_OAUTH_JWKS_URL": ISSUER + ".well-known/jwks.json", "MCP_RESOURCE_URL": RESOURCE,
       "MCP_OAUTH_ALLOWED_SUBJECTS": SUBJECT, "MCP_OAUTH_ALGORITHM": "RS256",
       "MCP_ALLOWED_HOSTS": "options.example.com", "RENDER_EXTERNAL_HOSTNAME": "",
       "THETADATA_API_KEY": "fake-provider-key", "MCP_API_KEY": "legacy-token-" + "x" * 32}
REAL_ASYNC_CLIENT = httpx.AsyncClient


class OAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, ENV))
        self.stack.enter_context(patch.object(mcp, "_session_manager", None))
        self.stack.enter_context(patch.object(mcp, "oauth_enabled", False))
        self.app = create_app()
        self.client = self.stack.enter_context(TestClient(self.app, base_url="https://options.example.com"))
        self.fetch = Mock(side_effect=lambda request: httpx.Response(200, json={"keys": [self.jwk(self.key)]}))
        self.stack.enter_context(patch("oauth.httpx.AsyncClient", side_effect=lambda **kwargs:
                                      REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self.fetch), **kwargs)))

    @staticmethod
    def jwk(key, kid="signing-key"):
        return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
                "kid": kid, "alg": "RS256", "use": "sig"}

    def token(self, *, key=None, headers=None, omit=(), **overrides):
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": RESOURCE, "sub": SUBJECT, "scope": "options:read",
                  "iat": now, "exp": now + 300, **overrides}
        for field in omit:
            claims.pop(field, None)
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers=headers or {"kid": "signing-key"})

    def rpc(self, token, method="tools/list", params=None, extra_headers=None):
        return self.client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                                "Accept": "application/json, text/event-stream", **(extra_headers or {})},
                                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_public_discovery_and_private_mcp(self):
        for path in (METADATA_PATH, "/.well-known/oauth-protected-resource"):
            response = self.client.get(path, headers={"Host": "untrusted.example"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"resource": RESOURCE, "authorization_servers": [ISSUER],
                             "scopes_supported": ["options:read"], "bearer_methods_supported": ["header"]})
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(self.client.head(path).status_code, 200)
            self.assertEqual(self.client.post(path).status_code, 401)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        for method in ("GET", "POST", "DELETE"):
            for path in ("/mcp", "/mcp/", "/mcp?access_token=ignored"):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401)
                self.assertIn(f'resource_metadata="https://options.example.com{METADATA_PATH}"',
                              response.headers["www-authenticate"])
                self.assertIn('scope="options:read"', response.headers["www-authenticate"])
        self.fetch.assert_not_called()

    def test_valid_token_initializes_lists_and_calls_without_provider_or_secret_leaks(self):
        token = self.token()
        self.assertEqual(self.rpc(token, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "oauth-test", "version": "1"}}).status_code, 200)
        tools = self.rpc(token).json()["result"]["tools"]
        self.assertEqual(len(tools), 4)
        for tool in tools:
            expected = [{"type": "oauth2", "scopes": ["options:read"]}]
            self.assertEqual(tool["securitySchemes"], expected)
            self.assertEqual(tool["_meta"]["securitySchemes"], expected)
        response = self.rpc(token, "tools/call", {"name": "get_status", "arguments": {}})
        self.assertEqual(response.status_code, 200)
        for secret in (token, ENV["MCP_API_KEY"], ENV["THETADATA_API_KEY"]):
            self.assertNotIn(secret, response.text)
        self.assertEqual(self.fetch.call_count, 1)
        request = self.fetch.call_args.args[0]
        self.assertEqual(str(request.url), ENV["MCP_OAUTH_JWKS_URL"])
        self.assertNotIn("authorization", request.headers)
        self.assertIn(self.rpc(token, extra_headers={"Host": "untrusted.example"}).status_code, (403, 421))
        self.assertEqual(self.rpc(token, extra_headers={"Origin": "https://untrusted.example"}).status_code, 403)

    def test_invalid_tokens_never_dispatch_tools(self):
        tokens = [ENV["MCP_API_KEY"], "not-a-jwt", self.token(key=self.other_key),
                  self.token(aud="https://vericite"), self.token(iss="https://other.example/"),
                  self.token(aud=ISSUER + "api/v2/"),
                  self.token(iss="https://dev-u3nmdx5lfnpnvmtm.us.auth0.com/"),
                  self.token(exp=int(time.time()) - 1), self.token(nbf=int(time.time()) + 600),
                  self.token(iat=int(time.time()) + 600), self.token(omit=("exp",)), self.token(omit=("sub",)),
                  self.token(exp="9999999999"), self.token(headers={"kid": "unknown", "jku": "https://attacker.example/"}),
                  jwt.encode({"aud": RESOURCE}, "x" * 32, algorithm="HS256", headers={"kid": "signing-key"}),
                  jwt.encode({"aud": RESOURCE}, "", algorithm="none", headers={"kid": "signing-key"})]
        with patch("main.execute", new_callable=AsyncMock) as execute:
            for token in tokens:
                with self.subTest(token_type=token[:12]):
                    response = self.rpc(token, "tools/call", {"name": "list_expirations", "arguments": {"symbol": "QQQ"}})
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn(token, response.text)
            execute.assert_not_awaited()
        self.assertEqual(self.fetch.call_count, 1)

    def test_private_account_and_scope_are_required(self):
        for claims, error in (({"sub": "auth0|someone-else"}, "access_denied"),
                              ({"scope": "other:read"}, "insufficient_scope"),
                              ({"scope": ["options:read"]}, "insufficient_scope")):
            response = self.rpc(self.token(**claims))
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"], error)
            self.assertIn("resource_metadata=", response.headers["www-authenticate"])

    def test_duplicate_headers_rejected_and_bearer_mode_cannot_bypass_oauth(self):
        token = self.token()
        response = self.client.post("/mcp", headers=[("Authorization", f"Bearer {token}")] * 2)
        self.assertEqual(response.status_code, 401)
        self.fetch.assert_not_called()
        self.assertEqual(self.rpc(ENV["MCP_API_KEY"]).status_code, 401)

    def test_jwks_outage_is_sanitized_fails_closed_and_is_throttled(self):
        self.fetch.side_effect = httpx.ConnectError("sensitive-upstream-details")
        for _ in range(2):
            response = self.rpc(self.token())
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"error": "temporarily_unavailable"})
            self.assertEqual(response.headers["retry-after"], "30")
        self.assertEqual(self.fetch.call_count, 1)

    def test_key_rotation_refresh_and_removed_key_rejection(self):
        self.assertEqual(self.rpc(self.token()).status_code, 200)
        self.fetch.side_effect = lambda request: httpx.Response(200, json={"keys": [self.jwk(self.other_key, "new-key")]})
        assert self.app.oauth is not None
        self.app.oauth._last_attempt -= 31
        new_token = self.token(key=self.other_key, headers={"kid": "new-key"})
        self.assertEqual(self.rpc(new_token).status_code, 200)
        self.assertEqual(self.rpc(self.token()).status_code, 401)
        self.assertEqual(self.fetch.call_count, 2)

    def test_expired_key_cache_is_not_used_during_outage(self):
        self.assertEqual(self.rpc(self.token()).status_code, 200)
        assert self.app.oauth is not None
        self.app.oauth._expires_at = 0
        self.app.oauth._last_attempt -= 31
        self.fetch.side_effect = httpx.ConnectError("down")
        self.assertEqual(self.rpc(self.token()).status_code, 503)

    def test_malformed_jwks_fails_closed(self):
        for document in ([], {}, {"keys": [None]}, {"keys": [{"kty": "RSA", "n": None}]}, {"keys": []}):
            assert self.app.oauth is not None
            self.app.oauth._last_attempt -= 31
            self.fetch.side_effect = lambda request: httpx.Response(200, json=document)
            self.assertEqual(self.rpc(self.token()).status_code, 503)

    def test_official_client_can_run_oauth_remote_check(self):
        # The test's lifespan is already active. This transport sends real MCP
        # initialize/list/call messages through the same OAuth middleware.
        with patch.dict(os.environ, {"MCP_OAUTH_ACCESS_TOKEN": self.token()}), \
             patch.object(check_remote, "AsyncClient", side_effect=lambda **kwargs:
                          REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=self.app), **kwargs)):
            result = asyncio.run(check_remote.check(RESOURCE, False, "QQQ", auth="oauth"))
        self.assertEqual(result["mcp_connection"], "passed")
        self.assertFalse(result["live_provider_checked"])

    def test_oauth_configuration_fails_closed(self):
        for override in ({"MCP_AUTH_MODE": "none"}, {"MCP_OAUTH_ISSUER": ""},
                         {"MCP_OAUTH_JWKS_URL": "http://issuer.example/keys"},
                         {"MCP_OAUTH_ALLOWED_SUBJECTS": ""}, {"MCP_OAUTH_ALLOWED_SUBJECTS": "*"},
                         {"MCP_RESOURCE_URL": RESOURCE + "/"}, {"MCP_RESOURCE_URL": RESOURCE + "?secret=x"},
                         {"MCP_RESOURCE_URL": "https://other.example/mcp"}, {"MCP_OAUTH_ALGORITHM": "HS256"},
                         {"THETADATA_API_KEY": ""}):
            with self.subTest(override=override), patch.dict(os.environ, override), self.assertRaises(ValueError):
                create_app()
        with patch.dict(os.environ, {"MCP_API_KEY": ""}):
            self.assertIsNotNone(create_app().oauth)
        self.assertEqual(OAuthSettings.from_env().issuer, ISSUER)

    def test_render_resource_default_and_explicit_custom_domain(self):
        with patch.dict(os.environ, {"MCP_RESOURCE_URL": "", "RENDER_EXTERNAL_HOSTNAME": "service.onrender.com"}):
            settings = OAuthSettings.from_env()
            self.assertEqual(settings.resource, "https://service.onrender.com/mcp")
            self.assertEqual(settings.metadata()["resource"], settings.resource)
            with patch.dict(os.environ, {"MCP_RESOURCE_URL": RESOURCE}):
                self.assertEqual(OAuthSettings.from_env().resource, RESOURCE)
        for host in ("", "bad.example/extra", "bad.example?query=x", "user:password@bad.example"):
            with patch.dict(os.environ, {"MCP_RESOURCE_URL": "", "RENDER_EXTERNAL_HOSTNAME": host}):
                with self.assertRaises(ValueError):
                    OAuthSettings.from_env()


if __name__ == "__main__":
    unittest.main()
