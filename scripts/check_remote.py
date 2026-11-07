"""Verify a hosted MCP endpoint; reads its bearer token only from MCP_API_KEY."""
import argparse
from datetime import datetime, timedelta
import asyncio
import json
import os
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent


async def check(url, live, symbol, greeks_model=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Use HTTPS, or HTTP only for a local loopback test")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Do not put credentials, tokens or query parameters in the endpoint URL")
    token = os.getenv("MCP_API_KEY")
    if not token:
        raise ValueError("Set MCP_API_KEY in your environment before running the check")
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}, timeout=60) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            expected = {"get_status", "list_expirations", "get_option_chain", "get_option_contract"}
            if {t.name for t in tools.tools} != expected:
                raise RuntimeError("The endpoint did not expose the expected ThetaData tools")
            status = await session.call_tool("get_status", {})
            if status.isError:
                raise RuntimeError("The remote status tool failed")
            if not status.content or not isinstance(status.content[0], TextContent):
                raise RuntimeError("The remote status tool did not return text content")
            payload = json.loads(status.content[0].text)
            if not payload.get("api_key_present"):
                raise RuntimeError("The deployed server is missing THETADATA_API_KEY")
            result = {"mcp_connection": "passed", "tools": sorted(expected), "provider_key_configured": True,
                      "live_provider_checked": live}
            if live:
                today = datetime.now(ZoneInfo("America/New_York")).date()
                expiration_result = await session.call_tool("list_expirations", {
                    "symbol": symbol, "start": today.isoformat(), "end": (today + timedelta(days=7)).isoformat()})
                if expiration_result.isError:
                    raise RuntimeError("The live expiration tool failed")
                if not expiration_result.content or not isinstance(expiration_result.content[0], TextContent):
                    raise RuntimeError("The live expiration tool did not return text content")
                expirations = json.loads(expiration_result.content[0].text)
                if "error" in expirations:
                    raise RuntimeError("Live provider check returned " + expirations["error"]["code"])
                chain_args = {"symbol": symbol, "max_dte": 5, "strike_range": 1, "max_rows": 10}
                if greeks_model is not None:
                    chain_args["greeks_model"] = greeks_model
                chain_result = await session.call_tool("get_option_chain", chain_args)
                if chain_result.isError:
                    raise RuntimeError("The live chain tool failed")
                if not chain_result.content or not isinstance(chain_result.content[0], TextContent):
                    raise RuntimeError("The live chain tool did not return text content")
                chain = json.loads(chain_result.content[0].text)
                if "error" in chain:
                    raise RuntimeError("Live provider check returned " + chain["error"]["code"])
                result.update(expiration_count=len(expirations.get("expirations", [])),
                              chain_rows=chain["returned_rows"], partial=chain["partial"],
                              source_status=chain["source_status"])
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Hosted HTTPS /mcp URL (or local loopback URL)")
    parser.add_argument("--live", action="store_true", help="Also call ThetaData expirations and a narrow option chain")
    parser.add_argument("--symbol", default="QQQ")
    parser.add_argument("--greeks-model", choices=("standard", "binomial"),
                        help="Select the first-order Greeks model for the live chain check")
    args = parser.parse_args()
    asyncio.run(check(args.url, args.live, args.symbol, args.greeks_model))
