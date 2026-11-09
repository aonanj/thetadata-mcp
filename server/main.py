"""Read-only tools shared by local stdio and hosted Streamable HTTP transports."""
from __future__ import annotations

from datetime import date
import importlib.metadata
import os
import re
import sys
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from runtime import execute
from horizon_gamma import GammaSettings

# Fail startup in both stdio and HTTP modes before accepting any tool calls.
GammaSettings.from_env()


class OptionsMCP(FastMCP):
    oauth_enabled = False

    async def list_tools(self):
        tools = await super().list_tools()
        if self.oauth_enabled:
            for tool in tools:
                schemes = [{"type": "oauth2", "scopes": ["options:read"]}]
                tool.meta = {**(tool.meta or {}), "securitySchemes": schemes}
                if tool.model_extra is not None:
                    tool.model_extra["securitySchemes"] = schemes
        return tools


mcp = OptionsMCP("ThetaData Options", instructions=(
    "Read-only options market data. Report source timestamps, missing fields and stale/crossed quote flags. "
    "Do not describe after-hours snapshots as current executable quotes. OI is daily, not intraday flow. "
    "IV/Greeks use native ThetaData units; quote/spread prices are dollars per share. "
    "horizon_gamma is a calculated Black-Scholes-Merton European-model scenario at a future time from the Greeks snapshot, "
    "with spot and IV held constant, in delta per dollar of underlying. Report its assumptions, timestamps and warnings. "
    "Never request the API key in chat. A subscription error is not a zero value."
), stateless_http=True, json_response=True, max_request_body_size=65536)
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def symbol_checked(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9./^_-]{0,19}", symbol):
        raise ValueError("Supply one underlying symbol, such as QQQ, SPY or AAPL.")
    return symbol


def date_checked(value: str) -> str:
    return date.fromisoformat(value).isoformat()


@mcp.tool(annotations=READ_ONLY)
def get_status() -> dict:
    """Check API-key presence and local dependencies without authenticating or revealing secrets."""
    packages = {}
    for name in ("thetadata", "mcp", "python-dotenv", "pandas_market_calendars"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"api_key_present": bool(os.getenv("THETADATA_API_KEY")), "packages": packages,
            "python_version": sys.version.split()[0],
            "supported_transports": ["stdio", "streamable-http"], "read_only": True,
            "connection_verified": False, "note": "Use list_expirations to test authenticated access."}


@mcp.tool(annotations=READ_ONLY)
async def list_expirations(symbol: str, start: str | None = None, end: str | None = None) -> dict:
    """List available option expirations for one underlying. Optional YYYY-MM-DD date bounds."""
    symbol = symbol_checked(symbol)
    start, end = date_checked(start) if start else None, date_checked(end) if end else None
    if start and end and start > end:
        raise ValueError("start must not be later than end")
    return await execute("expirations", {"symbol": symbol, "start": start, "end": end})


@mcp.tool(annotations=READ_ONLY)
async def get_option_chain(
    symbol: str,
    expiration: str = "*",
    right: Literal["call", "put", "both"] = "both",
    max_dte: Annotated[int | None, Field(ge=0, le=60)] = None,
    strike_range: Annotated[int, Field(ge=1, le=50)] = 5,
    include_greeks: bool = True,
    include_open_interest: bool = True,
    current_day_only: bool = False,
    max_rows: Annotated[int, Field(ge=1, le=500)] = 200,
    stale_after_seconds: Annotated[int, Field(ge=1, le=86400)] = 120,
    greeks_model: Literal["standard", "binomial"] = "standard",
) -> dict:
    """Get NBBO, spread, IV, Greeks and daily OI joined by exact contract identity.

    expiration is YYYY-MM-DD or '*'. For '*' max_dte defaults to 7 calendar days;
    explicit expirations ignore max_dte. strike_range selects N strikes each side of
    spot plus ATM. With include_greeks, greeks_model selects ThetaData's standard
    or binomial first-order Greeks and IV. Both routes are available on Standard.
    Partial results retain errors. Quotes may be up to 7 days old outside session.
    horizon_gamma is a separate calculated European-model scenario using constant
    spot/IV, configured carry and a future fraction of time from the Greeks timestamp.
    Inspect horizon_gamma_model and row warnings. include_greeks=false skips it.
    """
    args = locals().copy()
    args["symbol"] = symbol_checked(symbol)
    args["expiration"] = date_checked(expiration) if expiration != "*" else expiration
    return await execute("chain", args)


@mcp.tool(annotations=READ_ONLY)
async def get_option_contract(
    symbol: str,
    expiration: str,
    strike: Annotated[float, Field(gt=0, allow_inf_nan=False)],
    right: Literal["call", "put"],
    include_greeks: bool = True,
    include_open_interest: bool = True,
    current_day_only: bool = False,
    stale_after_seconds: Annotated[int, Field(ge=1, le=86400)] = 120,
    greeks_model: Literal["standard", "binomial"] = "standard",
) -> dict:
    """Fetch one exact option contract. Strike is dollars, e.g. 500.5; never mills.

    Returns bid/ask/size/spread plus independently timestamped IV, first-order
    Greeks and daily OI. Select standard or binomial first-order Greeks with greeks_model.
    Use this for a fresh check after discovering contracts with get_option_chain.
    Includes the same calculated horizon_gamma scenario and model metadata as the
    chain tool; its future time is anchored to the Greeks snapshot with spot/IV fixed.
    """
    args = locals().copy()
    args.update(symbol=symbol_checked(symbol), expiration=date_checked(expiration), strike=str(strike), max_rows=1)
    return await execute("chain", args)


if __name__ == "__main__":
    mcp.run(transport="stdio")
