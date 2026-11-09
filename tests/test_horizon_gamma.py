"""Numerical, calendar and MCP integration acceptance for horizon gamma."""
import json
import math
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from data import OptionsService, join_snapshots
from horizon_gamma import ET, YEAR_SECONDS, GammaSettings, SessionCloses, black_scholes_gamma, calculate_horizon_gamma
from main import get_option_chain, get_option_contract

NOW = datetime(2026, 11, 9, 17, tzinfo=timezone.utc)
SETTINGS = GammaSettings.from_env({})


def normalized(**updates):
    item = dict(symbol="QQQ", expiration="2026-11-09", strike=500.0, right="call",
                underlying_price=500.0, implied_vol=.25, greeks_timestamp=NOW.isoformat(),
                quote_timestamp=NOW.isoformat(), underlying_timestamp=NOW.isoformat(),
                spread_pct_of_mid=1.0, flags=[])
    item.update(updates)
    return item


def price(spot, strike, iv, years, rate, dividend, right="call"):
    # Independent price-based finite-difference oracle; intentionally no gamma helper.
    cdf = lambda x: (1 + math.erf(x / math.sqrt(2))) / 2
    d1 = (math.log(spot / strike) + (rate - dividend + .5 * iv**2) * years) / (iv * math.sqrt(years))
    d2 = d1 - iv * math.sqrt(years)
    if right == "call":
        return spot * math.exp(-dividend * years) * cdf(d1) - strike * math.exp(-rate * years) * cdf(d2)
    return strike * math.exp(-rate * years) * cdf(-d2) - spot * math.exp(-dividend * years) * cdf(-d1)


class MathTests(unittest.TestCase):
    def test_published_reference_and_nonzero_dividends(self):
        # MathWorks blsgamma(50, 50, .12, .25, .3, 0) = .0512 (published precision).
        self.assertAlmostEqual(black_scholes_gamma(50, 50, .3, .25, .12), .0512, delta=.00005)
        for years in (1 / 365, 5 / 365, 1 / YEAR_SECONDS):
            for right in ("call", "put"):
                s, k, iv, r, q = 500, 500, .25, .04, .012
                step = s * iv * math.sqrt(years) * .001
                expected = (price(s + step, k, iv, years, r, q, right)
                            - 2 * price(s, k, iv, years, r, q, right)
                            + price(s - step, k, iv, years, r, q, right)) / step**2
                self.assertAlmostEqual(black_scholes_gamma(s, k, iv, years, r, q) / expected, 1, delta=2e-4)

    def test_deep_tails_and_invalid_values(self):
        for spot in (1.0, 100000.0):
            self.assertEqual(black_scholes_gamma(spot, 500, .2, 1 / YEAR_SECONDS), 0)
        for invalid in (0, -1, float("nan"), float("inf")):
            for index in range(4):
                values = [500.0, 500.0, .2, 1 / 365]
                values[index] = invalid
                with self.assertRaises(ValueError):
                    black_scholes_gamma(*values)

    def test_provider_fixture_iv_is_decimal_without_rescaling(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/qqq_iv_units.json").read_text())
        row = fixture["row"]
        as_of = datetime.fromisoformat(row["greeks_timestamp"])
        terminal = datetime(2026, 11, 9, 16, 15, tzinfo=ET)
        years = (terminal.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)).total_seconds() / YEAR_SECONDS
        # A loose premium consistency check, NOT a claim to know the provider rate.
        approximate = price(row["underlying_price"], row["strike"], row["implied_vol"], years, .04, 0)
        self.assertLess(abs(approximate - (row["greeks_bid"] + row["greeks_ask"]) / 2), .25)
        for factor in (.01, 100):
            incorrect = price(row["underlying_price"], row["strike"], row["implied_vol"] * factor, years, .04, 0)
            self.assertGreater(abs(incorrect - 1.49), .5)
        result = calculate_horizon_gamma(row, settings=SETTINGS, closes=SessionCloses(), now=NOW)
        expected = black_scholes_gamma(row["underlying_price"], row["strike"], .0454, years * .75)
        self.assertEqual(result["value"], expected)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_overrides_and_negative_rates(self):
        self.assertEqual(SETTINGS.fraction, .25)
        settings = GammaSettings.from_env({"HORIZON_GAMMA_FRACTION": "0", "HORIZON_GAMMA_RISK_FREE_RATE": "-.01",
            "HORIZON_GAMMA_DIVIDEND_YIELD": ".02", "HORIZON_GAMMA_SYMBOL_PROFILES": '{"aapl":"regular_close","QQQ":"regular_close"}'})
        self.assertEqual(settings.symbol_profiles, {"QQQ": "regular_close", "SPY": "late_close", "AAPL": "regular_close"})
        self.assertEqual(settings.risk_free_rate, -.01)
        self.assertEqual(settings.dividend_yield, .02)
        self.assertEqual(settings.fraction, 0)

    def test_invalid_settings_do_not_echo_values(self):
        bad = {
            "HORIZON_GAMMA_FRACTION": ["", "secret-value", "NaN", "inf", "-0.1", "1", "2"],
            "HORIZON_GAMMA_RISK_FREE_RATE": ["NaN", "inf", "secret-value"],
            "HORIZON_GAMMA_DIVIDEND_YIELD": ["-0.1", "NaN", "inf"],
            "HORIZON_GAMMA_SYMBOL_PROFILES": ["secret-value", "[]", "null", '{"AAPL":"unknown"}', '{"AAPL":2}',
                                               '{"AAPL":[]}', '{"aapl":"regular_close","AAPL":"late_close"}', '{"bad key":"regular_close"}'],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError) as error:
                    GammaSettings.from_env({key: value})
                self.assertIn(key, str(error.exception))
                self.assertNotIn("secret-value", str(error.exception))

    def test_invalid_config_rejected_at_stdio_and_http_startup(self):
        for module in ("main", "http_app"):
            env = {**os.environ, "HORIZON_GAMMA_FRACTION": "secret-value"}
            result = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=Path(__file__).resolve().parents[1] / "server",
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("HORIZON_GAMMA_FRACTION must be", result.stderr)
            self.assertNotIn("secret-value", result.stderr)


class ScenarioTests(unittest.TestCase):
    def calculate(self, row=None, **kwargs):
        return calculate_horizon_gamma(row or normalized(), settings=kwargs.pop("settings", SETTINGS),
                                       closes=kwargs.pop("closes", SessionCloses()), now=kwargs.pop("now", NOW), **kwargs)

    def test_horizon_times_and_call_put_equality(self):
        result = self.calculate()
        self.assertEqual(result["seconds_to_expiry"], 4.25 * 3600)
        self.assertEqual(result["horizon_seconds"], 4.25 * 3600 / 4)
        self.assertEqual(result["remaining_seconds"], 4.25 * 3600 * .75)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], self.calculate(normalized(right="put"))["value"])
        zero = self.calculate(settings=replace(SETTINGS, fraction=0))
        self.assertEqual(zero["horizon_at"], NOW.isoformat())
        assert zero["value"] is not None
        self.assertAlmostEqual(zero["value"], black_scholes_gamma(500, 500, .25, 4.25 / 24 / 365))

    def test_fractional_last_seconds_use_actual_time_without_floor(self):
        stamp = datetime(2026, 11, 9, 21, 14, 59, 500000, tzinfo=timezone.utc)
        result = self.calculate(normalized(greeks_timestamp=stamp.isoformat()), now=stamp)
        self.assertEqual(result["seconds_to_expiry"], .5)
        self.assertEqual(result["remaining_seconds"], .375)
        self.assertIn("provider_iv_minimum_tte_may_apply", result["warnings"])
        assert result["value"] is not None
        self.assertAlmostEqual(result["value"], black_scholes_gamma(500, 500, .25, .375 / YEAR_SECONDS))

    def test_stale_future_missing_and_misaligned_timestamps(self):
        row = normalized(underlying_timestamp=(NOW + timedelta(seconds=180)).isoformat(),
                         quote_timestamp=(NOW - timedelta(seconds=180)).isoformat())
        result = self.calculate(row)
        for warning in ("future_underlying_timestamp", "stale_quote", "quote_greeks_time_gap", "underlying_greeks_time_gap", "quote_underlying_time_gap"):
            self.assertIn(warning, result["warnings"])
        self.assertIsNotNone(result["value"])
        self.assertIn("missing_underlying_timestamp", self.calculate(normalized(underlying_timestamp=None))["warnings"])
        future = self.calculate(normalized(greeks_timestamp=(NOW + timedelta(seconds=10)).isoformat()))
        self.assertIn("future_greeks_timestamp", future["warnings"])
        historical = self.calculate(now=NOW + timedelta(days=2))
        self.assertIn("horizon_in_past", historical["warnings"])
        self.assertIn("expired_since_snapshot", historical["warnings"])
        self.assertEqual(historical["value"], self.calculate()["value"])

    def test_unsupported_missing_expired_and_nonfinite_inputs(self):
        for updates, reason in (({"symbol": "SPX"}, "unsupported_symbol_profile"),
                                ({"greeks_timestamp": None}, "missing_greeks_timestamp"),
                                ({"expiration": "2026-11-08"}, "no_expiration_session"),
                                ({"expiration": "2026-11-06"}, "nonpositive_time_to_expiry")):
            result = self.calculate(normalized(**updates))
            self.assertEqual(result["reason"], reason)
            self.assertIsNone(result["value"])
        for name in ("underlying_price", "strike", "implied_vol"):
            for value in (None, 0, -1, float("nan"), float("inf")):
                result = self.calculate(normalized(**{name: value}))
                self.assertEqual(result["reason"], f"invalid_{name}")
                json.dumps(result, allow_nan=False)
        result = self.calculate(settings=replace(SETTINGS, risk_free_rate=1e308), row=normalized(implied_vol=1e-308))
        self.assertEqual(result["reason"], "numerical_failure")
        self.assertIsNone(result["value"])

    def test_quality_flags_and_snapshot_dte(self):
        row = normalized(expiration="2026-11-16", calendar_dte=0, spread_pct_of_mid=5.001,
                         flags=["crossed_quote", "zero_bid", "invalid_quote", "missing_open_interest"])
        result = self.calculate(row, greeks_model="binomial")
        for warning in ("outside_target_dte", "wide_spread", "crossed_quote", "zero_bid", "invalid_quote", "binomial_iv_in_european_model"):
            self.assertIn(warning, result["warnings"])
        self.assertNotIn("missing_open_interest", result["warnings"])
        self.assertNotIn("wide_spread", self.calculate(normalized(spread_pct_of_mid=5))["warnings"])


class CalendarTests(unittest.TestCase):
    def test_early_close_holiday_and_profiles(self):
        closes = SessionCloses()
        early = closes.get("2026-11-27")
        assert early is not None
        self.assertEqual(early.astimezone(ET).hour, 13)
        self.assertIsNone(closes.get("2026-11-26"))
        now = datetime(2026, 11, 27, 15, tzinfo=timezone.utc)
        row = normalized(expiration="2026-11-27", greeks_timestamp=now.isoformat())
        result = calculate_horizon_gamma(row, settings=SETTINGS, closes=closes, now=now)
        self.assertEqual(result["model_expiry_at"], "2026-11-27T18:15:00+00:00")
        other = GammaSettings.from_env({"HORIZON_GAMMA_SYMBOL_PROFILES": '{"AAPL":"regular_close"}'})
        result = calculate_horizon_gamma({**row, "symbol": "AAPL"}, settings=other, closes=closes, now=now)
        self.assertEqual(result["model_expiry_at"], "2026-11-27T18:00:00+00:00")

    def test_dst_weekend_uses_elapsed_utc_seconds(self):
        for start, end, hours in (("2026-03-06T16:15:00-05:00", "2026-03-09", 71),
                                  ("2026-10-30T16:15:00-04:00", "2026-11-02", 73)):
            now = datetime.fromisoformat(start)
            result = calculate_horizon_gamma(normalized(greeks_timestamp=start, expiration=end), settings=SETTINGS,
                                              closes=SessionCloses(), now=now)
            self.assertEqual(result["seconds_to_expiry"], hours * 3600)

    def test_calendar_resolves_a_date_only_once_per_request(self):
        import pandas_market_calendars as mcal
        with patch.object(mcal, "get_calendar", wraps=mcal.get_calendar) as get_calendar:
            closes = SessionCloses()
            for _ in range(3):
                closes.get("2026-11-09")
                closes.get("2026-11-08")
            self.assertEqual(get_calendar.call_count, 2)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def source(self, **updates):
        return dict(symbol="QQQ", expiration="2026-11-09", strike=500, right="call", timestamp=NOW.isoformat(),
                    bid=3.0, ask=3.02, implied_vol=.25, underlying_price=500,
                    underlying_timestamp=NOW.isoformat(), gamma=.123, **updates)

    async def test_both_tools_have_identical_output_with_identical_provider_inputs(self):
        class Frame:
            def __init__(self, row): self.row = row
            def to_dicts(self): return [self.row]

        class Client:
            def __init__(self, row): self.row, self.calls = row, []
            def snapshot(self, **params):
                self.calls.append(params)
                return Frame(self.row)
            option_snapshot_quote = snapshot
            option_snapshot_greeks_first_order = snapshot
            option_snapshot_binomial_greeks_first_order = snapshot

        for model in ("standard", "binomial"):
            client = Client(self.source())
            service = OptionsService(client)

            async def execute(operation, args):
                self.assertEqual(operation, "chain")
                return service.chain(**args)

            class Clock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return NOW.astimezone(tz)

            with patch("main.execute", side_effect=execute), patch("data.datetime", Clock):
                chain = await get_option_chain("QQQ", expiration="2026-11-09", greeks_model=model, include_open_interest=False)
                contract = await get_option_contract("QQQ", "2026-11-09", 500, "call", greeks_model=model, include_open_interest=False)
            self.assertEqual(chain["rows"], contract["rows"])
            self.assertEqual(chain["horizon_gamma_model"], contract["horizon_gamma_model"])
            self.assertFalse(chain["partial"])
            self.assertEqual(len(client.calls), 4)
            self.assertEqual(chain["rows"][0]["greeks"]["gamma"], .123)
            self.assertIsNotNone(chain["rows"][0]["horizon_gamma"]["value"])
            json.dumps(chain, allow_nan=False)

    async def test_disabled_and_missing_greeks_keep_rows_and_metadata(self):
        item = join_snapshots({"quotes": [self.source()]}, now=NOW)
        self.assertEqual(item["rows"][0]["horizon_gamma"]["reason"], "greeks_not_requested")
        self.assertEqual(item["horizon_gamma_model"]["calendar_version"], "5.6.0")
        missing = join_snapshots({"quotes": [self.source()], "greeks": []}, now=NOW)
        self.assertEqual(missing["returned_rows"], 1)
        self.assertEqual(missing["rows"][0]["horizon_gamma"]["reason"], "missing_greeks_timestamp")

    async def test_http_mcp_serializes_calculated_rows_for_both_tools(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.types import TextContent
        from http_app import create_app, mcp

        payload = join_snapshots({"quotes": [self.source()], "greeks": [self.source()]}, now=NOW)

        async def execute(operation, args):
            self.assertEqual(operation, "chain")
            return payload

        token = "gamma-test-token-" + "a" * 32
        with patch.dict(os.environ, {"MCP_AUTH_MODE": "bearer", "MCP_API_KEY": token, "THETADATA_API_KEY": "test-only"}), \
             patch.object(mcp, "_session_manager", None), \
             patch("main.execute", side_effect=execute):
            app = create_app()
            async with app.app.router.lifespan_context(app.app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Bearer {token}"}) as http:
                    async with streamable_http_client("http://localhost/mcp", http_client=http) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            for tool, args in (("get_option_chain", {"symbol": "QQQ"}),
                                               ("get_option_contract", {"symbol": "QQQ", "expiration": "2026-11-09", "strike": 500, "right": "call"})):
                                response = await session.call_tool(tool, args)
                                self.assertFalse(response.isError)
                                content = response.content[0]
                                assert isinstance(content, TextContent)
                                self.assertEqual(json.loads(content.text), payload)


if __name__ == "__main__":
    unittest.main()
