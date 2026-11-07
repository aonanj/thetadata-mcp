"""Verify a hosted MCP endpoint; reads its bearer token only from MCP_API_KEY."""
import argparse
from datetime import datetime, timedelta
import asyncio
import json
import os
import sys
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from httpx import AsyncClient, HTTPStatusError, RequestError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent


class CheckFailed(Exception):
    """An expected check failure with a credential-free diagnostic."""


def provider_failure(error):
    # Do not echo raw remote errors: they can contain provider credentials.
    hints = {
        "INTERNAL": "ThetaData returned an internal server error. Retry later; if it persists, contact ThetaData support with the endpoint and time.",
        "UNAVAILABLE": "ThetaData is unavailable. Retry later and check provider service status.",
        "NETWORK_ERROR": "The server could not reach ThetaData. Check its network and DNS access.",
        "DEADLINE_EXCEEDED": "ThetaData timed out. Retry with a narrower request.",
        "TIMEOUT": "The provider worker exceeded its time limit. Retry with a narrower request.",
        "BUSY": "The provider worker is busy. Retry after the current request finishes.",
        "AUTHENTICATION_FAILED": "Check THETADATA_API_KEY on the deployed server.",
        "UNAUTHENTICATED": "Check THETADATA_API_KEY on the deployed server.",
        "PERMISSION_DENIED": "Check the ThetaData subscription and endpoint entitlement.",
        "NO_DATA": "No matching records. Check the symbol, expiration and session.",
        "MISSING_API_KEY": "Set THETADATA_API_KEY on the deployed server.",
        "MISSING_DEPENDENCY": "Install the server requirements and redeploy.",
        "WORKER_FAILED": "The server's provider worker failed. Check its dependencies.",
        "INVALID_PROVIDER_RESPONSE": "The provider worker returned an invalid response.",
        "PROVIDER_ERROR": "ThetaData request failed. Check provider access and service status.",
    }
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or code not in hints:
        code = "PROVIDER_ERROR"
    return f"{code}: {hints[code]}"


async def tool_payload(session, name, arguments):
    result = await session.call_tool(name, arguments)
    if result.isError:
        raise CheckFailed(f"{name}: the remote MCP tool failed. Check its arguments and server logs.")
    if not result.content or not isinstance(result.content[0], TextContent):
        raise CheckFailed(f"{name}: the remote tool did not return text content.")
    try:
        payload = json.loads(result.content[0].text)
    except (ValueError, TypeError):
        raise CheckFailed(f"{name}: the remote tool returned invalid JSON.") from None
    if not isinstance(payload, dict):
        raise CheckFailed(f"{name}: the remote tool did not return a JSON object.")
    if "error" in payload:
        raise CheckFailed(f"{name}: {provider_failure(payload['error'])}")
    return payload


async def check(url, live, symbol, greeks_model=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise CheckFailed("Use HTTPS, or HTTP only for a local loopback test")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CheckFailed("Do not put credentials, tokens or query parameters in the endpoint URL")
    token = os.getenv("MCP_API_KEY")
    if not token:
        raise CheckFailed("Set MCP_API_KEY in your environment before running the check")
    async with (
        AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        expected = {"get_status", "list_expirations", "get_option_chain", "get_option_contract"}
        if {t.name for t in tools.tools} != expected:
            raise CheckFailed("The endpoint did not expose the expected ThetaData tools")
        payload = await tool_payload(session, "get_status", {})
        if not payload.get("api_key_present"):
            raise CheckFailed("The deployed server is missing THETADATA_API_KEY")
        result = {"mcp_connection": "passed", "tools": sorted(expected), "provider_key_configured": True,
                  "live_provider_checked": live}
        if live:
            today = datetime.now(ZoneInfo("America/New_York")).date()
            expirations = await tool_payload(session, "list_expirations", {
                "symbol": symbol, "start": today.isoformat(), "end": (today + timedelta(days=7)).isoformat()})
            if not expirations.get("expirations"):
                raise CheckFailed("list_expirations: NO_DATA: no expirations in the next seven days.")
            chain_args = {"symbol": symbol, "max_dte": 5, "strike_range": 1, "max_rows": 10}
            if greeks_model is not None:
                chain_args["greeks_model"] = greeks_model
            chain = await tool_payload(session, "get_option_chain", chain_args)
            failures = []
            for source in ("quotes", "greeks", "open_interest"):
                status = chain.get("source_status", {}).get(source, {})
                if status.get("status") == "error":
                    failures.append(f"{source}: {provider_failure(status)}")
                elif status.get("status") != "ok" or not status.get("row_count"):
                    failures.append(f"{source}: no provider records returned.")
            if failures:
                raise CheckFailed("get_option_chain: " + "; ".join(failures))
            if not chain.get("returned_rows"):
                raise CheckFailed("get_option_chain: NO_DATA: no matching option rows returned.")
            result.update(expiration_count=len(expirations.get("expirations", [])),
                          chain_rows=chain["returned_rows"], partial=chain["partial"],
                          source_status=chain["source_status"])
    return result


def failure_messages(error):
    # anyio wraps errors from async context managers in nested ExceptionGroups.
    if isinstance(error, BaseExceptionGroup):
        return [message for child in error.exceptions for message in failure_messages(child)]
    if isinstance(error, CheckFailed):
        return [str(error)]
    if isinstance(error, HTTPStatusError):
        code = error.response.status_code
        hint = "Check MCP_API_KEY matches the deployed service token." if code == 401 else "Check the endpoint and server logs."
        return [f"MCP HTTP {code}: {hint}"]
    if isinstance(error, RequestError):
        return ["MCP connection failed. Check the endpoint, network/DNS access and whether the service is awake."]
    return ["The remote check failed unexpectedly. Check server status and installed dependencies."]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Hosted HTTPS /mcp URL (or local loopback URL)")
    parser.add_argument("--live", action="store_true", help="Also call ThetaData expirations and a narrow option chain")
    parser.add_argument("--symbol", default="QQQ")
    parser.add_argument("--greeks-model", choices=("standard", "binomial"),
                        help="Select the first-order Greeks model for the live chain check")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(check(args.url, args.live, args.symbol, args.greeks_model))
    except Exception as error:
        print(json.dumps({"check": "failed", "errors": list(dict.fromkeys(failure_messages(error)))}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
