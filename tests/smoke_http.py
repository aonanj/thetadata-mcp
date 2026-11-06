"""Official MCP client through ASGI HTTP transport; no network/provider dependency."""
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from http_app import create_app


async def main():
    token = "test-only-http-client-token-" + "a" * 32
    with patch.dict(os.environ, {"MCP_API_KEY": token, "THETADATA_API_KEY": "fake-provider-key-for-tests"}):
        app = create_app()

        def factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     headers=headers, timeout=timeout, auth=auth)

        async with app.app.router.lifespan_context(app.app):
            async with streamablehttp_client("http://localhost/mcp", headers={"Authorization": f"Bearer {token}"},
                                             httpx_client_factory=factory) as (read, write, session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert len(tools.tools) == 4
                    result = await session.call_tool("get_status", {})
                    assert not result.isError
                    assert json.loads(result.content[0].text)["api_key_present"] is True
                    assert session_id() is None
                    print(json.dumps({"http_initialize": "passed", "http_discovery": "passed",
                                      "authenticated_tool_call": "passed", "stateless": True}))


asyncio.run(main())
