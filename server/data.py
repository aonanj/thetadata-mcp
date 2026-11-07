"""Provider adapter and timestamp-preserving option-chain joins."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import math
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KEYS = ("symbol", "expiration", "strike", "right")
GREEKS = ("delta", "gamma", "theta", "vega", "rho", "epsilon", "lambda",
          "vanna", "charm", "vomma", "veta", "vera", "speed", "zomma",
          "color", "ultima", "d1", "d2", "dual_delta", "dual_gamma")


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return result.replace(tzinfo=ET) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def iso(value: Any) -> str | None:
    result = timestamp(value)
    return result.isoformat() if result else None


def expiry(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def key(row: dict) -> tuple:
    strike = Decimal(str(row["strike"]))
    if not strike.is_finite():
        raise ValueError("Nonfinite strike")
    right = {"c": "call", "p": "put"}.get(str(row["right"]).lower(), str(row["right"]).lower())
    if right not in ("call", "put"):
        raise ValueError("Unknown option right")
    return str(row["symbol"]).upper(), expiry(row["expiration"]), strike, right


def indexed(rows: list[dict]) -> tuple[dict, int]:
    result, invalid = {}, 0
    for row in rows:
        try:
            identity = key(row)
        except (KeyError, ValueError, TypeError, InvalidOperation):
            invalid += 1
            continue
        old = result.get(identity)
        floor = datetime.min.replace(tzinfo=timezone.utc)
        if old is None or (timestamp(row.get("timestamp")) or floor) >= (timestamp(old.get("timestamp")) or floor):
            result[identity] = row
    return result, invalid


def age(value: Any, now: datetime) -> float | None:
    stamp = timestamp(value)
    return round((now - stamp).total_seconds(), 3) if stamp else None


def join_snapshots(sources: dict[str, list[dict]], *, max_rows: int = 200,
                   stale_after_seconds: int = 120, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    maps, invalid = {}, {}
    for name, records in sources.items():
        maps[name], invalid[name] = indexed(records)
    identities = set().union(*(set(m) for m in maps.values())) if maps else set()
    ordered = sorted(identities, key=lambda k: (k[1], k[2], k[3], k[0]))
    output = []
    for symbol, expiration, strike, right in ordered[:max_rows]:
        identity = (symbol, expiration, strike, right)
        q = maps.get("quotes", {}).get(identity, {})
        g = maps.get("greeks", {}).get(identity, {})
        oi = maps.get("open_interest", {}).get(identity, {})
        bid, ask = number(q.get("bid")), number(q.get("ask"))
        flags = []
        if not q:
            flags.append("missing_quote")
        if "greeks" in maps and not g:
            flags.append("missing_greeks")
        if "open_interest" in maps and not oi:
            flags.append("missing_open_interest")
        if bid is None or ask is None:
            flags.append("incomplete_quote")
        elif bid < 0 or ask <= 0:
            flags.append("invalid_quote")
        elif bid > ask:
            flags.append("crossed_quote")
        elif bid == 0:
            flags.append("zero_bid")
        if bid is not None and ask is not None and 0 <= bid <= ask and ask > 0:
            mid = (bid + ask) / 2
            spread = ask - bid
        else:
            mid = spread = None
        quote_age, greeks_age = age(q.get("timestamp"), now), age(g.get("timestamp"), now)
        for label, record, seconds in (("quote", q, quote_age), ("greeks", g, greeks_age)):
            if record and seconds is None:
                flags.append(f"unknown_{label}_time")
            elif seconds is not None and seconds > stale_after_seconds:
                flags.append(f"stale_{label}")
            elif seconds is not None and seconds < -5:
                flags.append(f"future_{label}_timestamp")
        gt, qt = timestamp(g.get("timestamp")), timestamp(q.get("timestamp"))
        output.append({
            "symbol": symbol, "expiration": expiration, "strike": float(strike), "right": right,
            "calendar_dte": (date.fromisoformat(expiration) - now.astimezone(ET).date()).days,
            "bid": bid, "ask": ask, "mid": round(mid, 8) if mid is not None else None,
            "spread": round(spread, 8) if spread is not None else None,
            "spread_pct_of_mid": round(100 * spread / mid, 4) if mid and spread is not None else None,
            "bid_size": q.get("bid_size"), "ask_size": q.get("ask_size"),
            "quote_timestamp": iso(q.get("timestamp")), "quote_age_seconds": quote_age,
            "greeks": {name: number(g.get(name)) for name in GREEKS},
            "implied_vol": number(g.get("implied_vol")), "iv_error": number(g.get("iv_error")),
            "greeks_timestamp": iso(g.get("timestamp")), "greeks_age_seconds": greeks_age,
            "greeks_bid": number(g.get("bid")), "greeks_ask": number(g.get("ask")),
            "quote_greeks_time_gap_seconds": round(abs((gt - qt).total_seconds()), 3) if gt and qt else None,
            "underlying_price": number(g.get("underlying_price")),
            "underlying_timestamp": iso(g.get("underlying_timestamp")),
            "open_interest": oi.get("open_interest"),
            "open_interest_timestamp": iso(oi.get("timestamp")),
            "flags": flags,
        })
    return {"rows": output, "returned_rows": len(output), "total_matching_rows": len(ordered),
            "truncated": len(ordered) > max_rows, "invalid_provider_rows": invalid}


def provider_error(error: Exception) -> dict:
    """Return categories, never provider messages that can contain credentials."""
    name = type(error).__name__
    code = "PROVIDER_ERROR"
    if name == "NoDataFoundError":
        code = "NO_DATA"
    elif name == "AuthenticationError":
        code = "AUTHENTICATION_FAILED"
    elif name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "ProxyError"):
        code = "NETWORK_ERROR"
    elif callable(code_method := getattr(error, "code", None)):
        try:
            code = getattr(code_method(), "name")
        except Exception:
            pass
    hints = {
        "NO_DATA": "No matching provider records. Check expiration, session and current_day_only.",
        "AUTHENTICATION_FAILED": "ThetaData rejected authentication. Check THETADATA_API_KEY locally.",
        "PERMISSION_DENIED": "ThetaData denied access. Check the subscription and entitlement for the requested endpoint.",
        "UNAUTHENTICATED": "ThetaData authentication is required or expired.",
        "UNAVAILABLE": "ThetaData is unreachable. Check connectivity and provider service status.",
        "NETWORK_ERROR": "Unable to connect to ThetaData. Check network and DNS access.",
        "DEADLINE_EXCEEDED": "ThetaData request timed out. Narrow the expiration or strike range.",
    }
    return {"code": code, "message": hints.get(code, "ThetaData request failed; check access, inputs and provider status.")}


class OptionsService:
    def __init__(self, client):
        self.client = client

    def expirations(self, symbol: str, start: str | None = None, end: str | None = None) -> dict:
        rows = self.client.option_list_expirations(symbol=symbol).to_dicts()
        dates = sorted({expiry(r["expiration"]) for r in rows})
        if start:
            dates = [d for d in dates if d >= start]
        if end:
            dates = [d for d in dates if d <= end]
        return {"symbol": symbol, "expirations": dates}

    def chain(self, symbol: str, expiration: str = "*", right: str = "both",
              strike: str = "*", max_dte: int | None = None, strike_range: int = 5,
              include_greeks: bool = True, include_open_interest: bool = True,
              current_day_only: bool = False, max_rows: int = 200,
              stale_after_seconds: int = 120, greeks_model: str = "standard") -> dict:
        params = {"symbol": symbol, "expiration": expiration, "right": right, "strike": strike,
                  "current_day_only": current_day_only}
        # SDK 1.0.17 drops max_dte=0/strike_range=0 with truthiness checks.
        # For 0DTE pass today's actual expiration, never rely on max_dte=0.
        if max_dte == 0 and expiration == "*":
            params["expiration"] = datetime.now(ET).date().isoformat()
        elif max_dte is not None and expiration == "*":
            params["max_dte"] = max_dte
        elif expiration == "*":
            params["max_dte"] = 7
        if strike == "*":
            params["strike_range"] = strike_range
        methods = {"quotes": "option_snapshot_quote"}
        if include_greeks:
            methods["greeks"] = {
                "standard": "option_snapshot_greeks_first_order",
                "binomial": "option_snapshot_binomial_greeks_first_order",
            }[greeks_model]
        if include_open_interest:
            methods["open_interest"] = "option_snapshot_open_interest"
        sources, status = {}, {}
        for source, method in methods.items():
            try:
                sources[source] = getattr(self.client, method)(**params).to_dicts()
                status[source] = {"status": "ok" if sources[source] else "empty", "row_count": len(sources[source])}
            except Exception as error:
                failure = provider_error(error)
                status[source] = {"status": "empty" if failure["code"] == "NO_DATA" else "error", **failure}
                sources[source] = []
            if source == "greeks":
                status[source].update(model=greeks_model, endpoint=method)
        result = join_snapshots(sources, max_rows=max_rows, stale_after_seconds=stale_after_seconds)
        result.update({"source_status": status, "effective_provider_filters": params,
                       "partial": any(v["status"] != "ok" for v in status.values()),
                       "retrieved_at": datetime.now(timezone.utc).isoformat(),
                       "timestamp_timezone": "America/New_York for naive provider timestamps",
                       "notes": [
                           "Snapshots can contain the latest record from the past seven days; inspect source timestamps and flags.",
                           "Open interest is a daily report reflecting the previous trading day's positions; it is not intraday flow.",
                           "Sources are fetched separately and are not an atomic snapshot. Greeks retain their own bid/ask and timestamp.",
                           "Prices and spreads are dollars per share. IV and Greeks retain ThetaData's native units; no percent or contract scaling is applied.",
                           "Rows are sorted by expiration, strike and right; truncation is not a ranking of candidates.",
                       ]})
        return result
