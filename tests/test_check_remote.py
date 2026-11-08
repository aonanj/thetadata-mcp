"""Run the CLI through the real MCP HTTP stack with controlled provider responses."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))
import check_remote
from http_app import create_app, mcp


def chain_payload():
    return {"returned_rows": 2, "partial": False, "source_status": {
        name: {"status": "ok", "row_count": 2} for name in ("quotes", "greeks", "open_interest")}}


class RemoteCheckTests(unittest.TestCase):
    def run_cli(self, responses, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        real_check = check_remote.check
        # The SDK session manager can run only once; each CLI invocation gets
        # an isolated manager, just as separate deployed processes would.
        with patch.object(mcp, "_session_manager", None), \
             patch.dict(os.environ, {"MCP_API_KEY": "test-token-" + "a" * 32,
                                     "THETADATA_API_KEY": "secret-provider-key"}):
            app = create_app()

            def client_factory(**kwargs):
                return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

            async def hosted_check(*args):
                async with app.app.router.lifespan_context(app.app):
                    return await real_check(*args)

            with patch("main.execute", new_callable=AsyncMock, side_effect=responses) as execute, \
                 patch.object(check_remote, "AsyncClient", side_effect=client_factory), \
                 patch.object(check_remote, "check", side_effect=hosted_check), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = check_remote.main(["--url", "http://localhost/mcp", *extra])
        return exit_code, stdout.getvalue(), stderr.getvalue(), execute

    def test_nonlive_checks_mcp_without_querying_provider(self):
        code, out, err, execute = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(json.loads(out)["live_provider_checked"])
        execute.assert_not_awaited()

    def test_live_success_checks_all_sources_and_forwards_model(self):
        code, out, err, execute = self.run_cli(
            [{"expirations": ["2026-11-09"]}, chain_payload()], "--live", "--greeks-model", "binomial")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["chain_rows"], 2)
        self.assertEqual(execute.await_args_list[1].args[1]["greeks_model"], "binomial")

    def test_internal_error_unwraps_actual_mcp_exception_groups(self):
        code, out, err, execute = self.run_cli(
            [{"error": {"code": "INTERNAL", "message": "secret-provider-key"}}], "--live")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("list_expirations: INTERNAL", json.loads(err)["errors"][0])
        self.assertIn("ThetaData returned an internal server error", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("ExceptionGroup", err)
        self.assertNotIn("secret-provider-key", err)
        self.assertEqual(execute.await_count, 1)

    def test_partial_chain_errors_fail_instead_of_reporting_success(self):
        chain = chain_payload()
        chain["partial"] = True
        for source in ("greeks", "open_interest"):
            chain["source_status"][source] = {"status": "error", "code": "INTERNAL", "message": "secret-provider-key"}
        code, out, err, _ = self.run_cli([{"expirations": ["2026-11-09"]}, chain], "--live")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        message = json.loads(err)["errors"][0]
        self.assertIn("get_option_chain", message)
        self.assertIn("greeks: INTERNAL", message)
        self.assertIn("open_interest: INTERNAL", message)
        self.assertNotIn("secret-provider-key", err)

    def test_empty_expirations_or_sources_fail_live_check(self):
        chain = chain_payload()
        chain["partial"] = True
        chain["source_status"]["quotes"] = {"status": "empty", "row_count": 0}
        for responses in ([{"expirations": []}], [{"expirations": ["2026-11-09"]}, chain]):
            with self.subTest(responses=responses):
                code, out, err, _ = self.run_cli(responses, "--live")
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(json.loads(err)["check"], "failed")

    def test_missing_token_has_actionable_error_without_traceback(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"MCP_API_KEY": ""}), redirect_stderr(stderr):
            code = check_remote.main(["--url", "https://example.com/mcp"])
        self.assertEqual(code, 1)
        self.assertIn("Set MCP_API_KEY", json.loads(stderr.getvalue())["errors"][0])

    def test_oauth_check_requires_oauth_token_without_legacy_fallback(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"MCP_API_KEY": "legacy-token", "MCP_OAUTH_ACCESS_TOKEN": ""}), redirect_stderr(stderr):
            code = check_remote.main(["--url", "https://example.com/mcp", "--auth", "oauth"])
        self.assertEqual(code, 1)
        self.assertIn("Set MCP_OAUTH_ACCESS_TOKEN", json.loads(stderr.getvalue())["errors"][0])

    def test_unknown_provider_codes_and_transport_errors_do_not_leak_details(self):
        self.assertNotIn("secret", check_remote.provider_failure({"code": "secret", "message": "secret"}))
        request = httpx.Request("POST", "https://example.com/mcp")
        error = ExceptionGroup("secret", [ExceptionGroup("secret", [
            httpx.HTTPStatusError("secret", request=request, response=httpx.Response(401)),
            httpx.ConnectError("secret"), RuntimeError("secret"),
        ])])
        messages = check_remote.failure_messages(error)
        self.assertEqual(len(messages), 3)
        self.assertIn("MCP HTTP 401", messages[0])
        self.assertIn("network/DNS", messages[1])
        self.assertNotIn("secret", str(messages))


if __name__ == "__main__":
    unittest.main()
