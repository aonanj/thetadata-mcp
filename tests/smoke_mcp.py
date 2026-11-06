"""Exercise MCP initialize, discovery, a tool call, validation and secret absence."""
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop("THETADATA_API_KEY", None)
    params = StdioServerParameters(command=sys.executable, args=[str(root / "server/main.py")], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 4
            status = await session.call_tool("get_status", {})
            assert not status.isError
            value = json.loads(status.content[0].text)
            assert value["api_key_present"] is False
            missing = await session.call_tool("list_expirations", {"symbol": "QQQ"})
            assert "MISSING_API_KEY" in missing.content[0].text
            invalid = await session.call_tool("get_option_chain", {"symbol": "QQQ", "max_dte": -1})
            assert invalid.isError
            print(json.dumps({"initialize": "passed", "tools": [t.name for t in tools.tools],
                              "status": "passed", "missing_key": "passed", "input_validation": "passed"}))


asyncio.run(main())
