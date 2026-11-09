"""Fixed-input Black–Scholes–Merton scenarios; no market-data requests."""
from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version
from types import MappingProxyType
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
YEAR_SECONDS = 365 * 86400
DEFAULT_PROFILES = {"QQQ": "late_close", "SPY": "late_close"}


class HorizonGamma(TypedDict):
    value: float | None
    status: Literal["ok", "warning", "unavailable"]
    reason: str | None
    warnings: list[str]
    as_of: str | None
    horizon_at: str | None
    model_expiry_at: str | None
    seconds_to_expiry: float | None
    horizon_seconds: float | None
    remaining_seconds: float | None


@dataclass(frozen=True)
class GammaSettings:
    fraction: float = 0.25
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    symbol_profiles: Mapping[str, str] = field(default_factory=lambda: MappingProxyType(DEFAULT_PROFILES.copy()))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GammaSettings:
        env = os.environ if env is None else env

        def numeric(name: str, default: str) -> float:
            try:
                value = float(env.get(name, default))
                if math.isfinite(value):
                    return value
            except (ValueError, TypeError, OverflowError):
                pass
            raise ValueError(f"{name} must be a finite decimal number")

        fraction = numeric("HORIZON_GAMMA_FRACTION", "0.25")
        rate = numeric("HORIZON_GAMMA_RISK_FREE_RATE", "0.0")
        dividend = numeric("HORIZON_GAMMA_DIVIDEND_YIELD", "0.0")
        if not 0 <= fraction < 1:
            raise ValueError("HORIZON_GAMMA_FRACTION must satisfy 0 <= fraction < 1")
        if dividend < 0:
            raise ValueError("HORIZON_GAMMA_DIVIDEND_YIELD must be nonnegative")
        profiles = DEFAULT_PROFILES.copy()
        try:
            extra = json.loads(env.get("HORIZON_GAMMA_SYMBOL_PROFILES", "{}"))
            if not isinstance(extra, dict):
                raise ValueError
            seen = set()
            for symbol, profile in extra.items():
                normalized = symbol.strip().upper()
                if (not re.fullmatch(r"[A-Z0-9][A-Z0-9./^_-]{0,19}", normalized)
                        or profile not in ("regular_close", "late_close") or normalized in seen):
                    raise ValueError
                seen.add(normalized)
                profiles[normalized] = profile
        except (ValueError, TypeError, AttributeError):
            raise ValueError("HORIZON_GAMMA_SYMBOL_PROFILES must be a JSON object mapping unique symbols to regular_close or late_close") from None
        return cls(fraction, rate, dividend, MappingProxyType(profiles))

    def metadata(self) -> dict:
        return {
            "model": "black_scholes_merton", "version": "1.0.0",
            "horizon_fraction": self.fraction, "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "carry_source": "configured_assumptions_not_provider_rates",
            "scenario": "constant_spot_iv_and_carry_from_greeks_snapshot",
            "iv_convention": "annualized_decimal", "time_basis": "ACT/365F",
            "units": "delta_per_usd_underlying", "contract_multiplier_applied": False,
            "calendar": "NYSE", "calendar_version": version("pandas_market_calendars"),
            "symbol_profiles": dict(self.symbol_profiles),
            "expiry_convention": "session_close_plus_profile_offset_not_exercise_deadline",
            "profile_timezone": "America/New_York", "wide_spread_pct_threshold": 5.0,
            "limitations": ["european_model", "no_discrete_dividend_or_early_exercise_model",
                            "provider_iv_time_convention_may_differ", "liquidity_not_certified"],
        }


def black_scholes_gamma(spot: float, strike: float, iv: float, years: float,
                       rate: float = 0.0, dividend: float = 0.0) -> float:
    """Gamma per dollar of spot, using annualized decimal IV and continuous carry.

    Log-domain evaluation avoids premature density/denominator underflow. A
    genuine deep-tail underflow may be zero; invalid inputs raise, never return 0.
    """
    if not all(math.isfinite(v) and v > 0 for v in (spot, strike, iv, years)):
        raise ValueError("Spot, strike, IV and remaining years must be finite and positive")
    if not all(math.isfinite(v) for v in (rate, dividend)) or dividend < 0:
        raise ValueError("Invalid carry inputs")
    root_t = math.sqrt(years)
    scale = iv * root_t
    d1 = (math.log(spot) - math.log(strike)) / scale + ((rate - dividend) / iv + iv / 2) * root_t
    if not math.isfinite(d1) or not math.isfinite(scale) or not math.isfinite(dividend * years):
        raise ValueError("Nonfinite intermediate result")
    log_gamma = -dividend * years - 0.5 * d1 * d1 - math.log(spot) - math.log(scale) - 0.5 * math.log(2 * math.pi)
    gamma = math.exp(log_gamma)
    if not math.isfinite(gamma):
        raise ValueError("Nonfinite gamma")
    return gamma


class SessionCloses:
    """Request-local cache: each expiration date is resolved at most once."""

    def __init__(self):
        self._closes: dict[str, datetime | None] = {}

    def get(self, expiration: str) -> datetime | None:
        if expiration not in self._closes:
            import pandas_market_calendars as mcal

            schedule = mcal.get_calendar("NYSE").schedule(start_date=expiration, end_date=expiration)
            self._closes[expiration] = None if schedule.empty else schedule.iloc[0]["market_close"].to_pydatetime()
        return self._closes[expiration]


def _stamp(value) -> datetime | None:
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ET)
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _positive(value) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def calculate_horizon_gamma(row: dict, *, settings: GammaSettings, closes: SessionCloses,
                            now: datetime, include_greeks: bool = True,
                            greeks_model: str = "standard", stale_after_seconds: int = 120) -> HorizonGamma:
    """Enrich a normalized row without modifying it or its provider diagnostics."""
    result = HorizonGamma(value=None, status="unavailable", reason=None, warnings=[], as_of=None,
                          horizon_at=None, model_expiry_at=None, seconds_to_expiry=None,
                          horizon_seconds=None, remaining_seconds=None)
    warnings = result["warnings"]

    def unavailable(reason: str) -> HorizonGamma:
        result["reason"] = reason
        return result

    if not include_greeks:
        return unavailable("greeks_not_requested")
    as_of = _stamp(row.get("greeks_timestamp"))
    if as_of is None:
        return unavailable("missing_greeks_timestamp")
    result["as_of"] = as_of.isoformat()
    for key in ("underlying_price", "strike", "implied_vol"):
        if not _positive(row.get(key)):
            return unavailable(f"invalid_{key}")
    profile = settings.symbol_profiles.get(row["symbol"])
    if profile is None:
        return unavailable("unsupported_symbol_profile")
    try:
        close = closes.get(row["expiration"])
    except (ValueError, OverflowError, KeyError):
        return unavailable("invalid_expiration_session")
    if close is None:
        return unavailable("no_expiration_session")
    model_expiry = close.astimezone(timezone.utc) + timedelta(minutes=15 if profile == "late_close" else 0)
    result["model_expiry_at"] = model_expiry.isoformat()
    seconds = (model_expiry - as_of).total_seconds()
    result["seconds_to_expiry"] = seconds
    if seconds <= 0:
        return unavailable("nonpositive_time_to_expiry")
    horizon_seconds = settings.fraction * seconds
    remaining = (1 - settings.fraction) * seconds
    horizon_at = as_of + timedelta(seconds=horizon_seconds)
    result.update(horizon_at=horizon_at.isoformat(), horizon_seconds=horizon_seconds, remaining_seconds=remaining)

    # Preserve quote/Greek quality signals without treating daily OI as an input.
    warnings.extend(flag for flag in row.get("flags", []) if flag not in ("missing_open_interest", "missing_greeks"))
    now = now.astimezone(timezone.utc)
    stamps = {"greeks": as_of, "quote": _stamp(row.get("quote_timestamp")),
              "underlying": _stamp(row.get("underlying_timestamp"))}
    for name, stamp in stamps.items():
        if stamp is None:
            warnings.append(f"missing_{name}_timestamp")
        elif (now - stamp).total_seconds() > stale_after_seconds:
            warnings.append(f"stale_{name}")
        elif (stamp - now).total_seconds() > 5:
            warnings.append(f"future_{name}_timestamp")
    for left, right in (("quote", "greeks"), ("underlying", "greeks"), ("quote", "underlying")):
        a, b = stamps[left], stamps[right]
        if a is not None and b is not None and abs((a - b).total_seconds()) > stale_after_seconds:
            warnings.append(f"{left}_{right}_time_gap")
    if horizon_at < now:
        warnings.append("horizon_in_past")
    if model_expiry <= now:
        warnings.append("expired_since_snapshot")
    if not 0 <= (date.fromisoformat(row["expiration"]) - as_of.astimezone(ET).date()).days <= 5:
        warnings.append("outside_target_dte")
    spread = row.get("spread_pct_of_mid")
    if isinstance(spread, (int, float)) and spread > 5:
        warnings.append("wide_spread")
    if seconds <= 3600:
        warnings.append("provider_iv_minimum_tte_may_apply")
    if greeks_model == "binomial":
        warnings.append("binomial_iv_in_european_model")
    result["warnings"] = list(dict.fromkeys(warnings))
    try:
        result["value"] = black_scholes_gamma(row["underlying_price"], row["strike"], row["implied_vol"],
                                              remaining / YEAR_SECONDS, settings.risk_free_rate, settings.dividend_yield)
    except (ValueError, OverflowError, ZeroDivisionError):
        return unavailable("numerical_failure")
    result["status"] = "warning" if result["warnings"] else "ok"
    return result
