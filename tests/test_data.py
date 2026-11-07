import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from data import OptionsService, join_snapshots, provider_error
from main import mcp, symbol_checked

NOW = datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc)


def row(**updates):
    result = dict(symbol="QQQ", expiration="2026-11-06", strike=500.5,
                  right="call", timestamp="2026-11-06T11:00:00", bid=3.0, ask=3.2)
    result.update(updates)
    return result


class Frame:
    def __init__(self, records):
        self.records = records

    def to_dicts(self):
        return self.records


class FakeClient:
    def __init__(self):
        self.calls = []

    def option_snapshot_quote(self, **kwargs):
        self.calls.append(kwargs)
        return Frame([row()])

    def option_snapshot_greeks_first_order(self, **kwargs):
        raise RuntimeError("secret-should-never-appear")

    def option_snapshot_open_interest(self, **kwargs):
        return Frame([row(open_interest=123)])


class ProviderRPCError(grpc.RpcError):
    def __init__(self, code):
        super().__init__("secret-provider-detail")
        self.status_code = code

    def code(self):
        return self.status_code


class DataTests(unittest.TestCase):
    def join(self, sources, **kwargs):
        return join_snapshots(sources, now=NOW, **kwargs)

    def test_spread_and_decimal_strike(self):
        result = self.join({"quotes": [row()], "greeks": [row(strike="500.500", theta="-1.2", delta=.52)]})
        item = result["rows"][0]
        self.assertEqual(result["returned_rows"], 1)
        self.assertEqual(item["spread"], .2)
        self.assertAlmostEqual(item["spread_pct_of_mid"], 6.4516)
        self.assertEqual(item["greeks"]["theta"], -1.2)
        self.assertTrue(item["quote_timestamp"].endswith("-05:00"))

    def test_exact_identity_prevents_wrong_expiration_or_right_join(self):
        result = self.join({"quotes": [row()], "greeks": [row(expiration="2026-11-09", delta=.4), row(right="put", delta=-.5)]})
        self.assertEqual(result["returned_rows"], 3)
        call = next(r for r in result["rows"] if r["expiration"] == "2026-11-06" and r["right"] == "call")
        self.assertIsNone(call["greeks"]["delta"])

    def test_source_timestamps_do_not_overwrite_quote(self):
        result = self.join({"quotes": [row()], "greeks": [row(timestamp="2026-11-06T10:59:00", bid=2.9)],
                            "open_interest": [row(timestamp="2026-11-06T06:30:00", open_interest=123)]})
        item = result["rows"][0]
        self.assertEqual(item["bid"], 3)
        self.assertEqual(item["greeks_bid"], 2.9)
        self.assertEqual(item["quote_greeks_time_gap_seconds"], 60)
        self.assertEqual(item["open_interest"], 123)
        self.assertNotEqual(item["quote_timestamp"], item["open_interest_timestamp"])

    def test_crossed_and_missing_quotes(self):
        item = self.join({"quotes": [row(bid=4, ask=3)]})["rows"][0]
        self.assertIn("crossed_quote", item["flags"])
        self.assertIsNone(item["spread_pct_of_mid"])
        missing = self.join({"open_interest": [row(open_interest=12)]})["rows"][0]
        self.assertIsNone(missing["bid"])
        self.assertIn("missing_quote", missing["flags"])

    def test_stale_quotes_and_nonfinite_greeks(self):
        item = self.join({"quotes": [row(timestamp="2026-11-05T16:00:00")],
                          "greeks": [row(delta=float("nan"), implied_vol=float("inf"))]})["rows"][0]
        self.assertIn("stale_quote", item["flags"])
        self.assertIsNone(item["greeks"]["delta"])
        self.assertIsNone(item["implied_vol"])

    def test_duplicate_uses_latest_quote(self):
        item = self.join({"quotes": [row(), row(timestamp="2026-11-06T10:00:00", bid=1)]})["rows"][0]
        self.assertEqual(item["bid"], 3)

    def test_bad_rows_and_truncation_are_visible(self):
        result = self.join({"quotes": [row(), row(strike=501), {"bad": "record"}]}, max_rows=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_matching_rows"], 2)
        self.assertEqual(result["invalid_provider_rows"]["quotes"], 1)

    def test_partial_access_keeps_quotes_and_oi_without_leaking_errors(self):
        result = OptionsService(FakeClient()).chain("QQQ", expiration="2026-11-06")
        self.assertTrue(result["partial"])
        self.assertEqual(result["rows"][0]["bid"], 3)
        self.assertEqual(result["rows"][0]["open_interest"], 123)
        self.assertIsNone(result["rows"][0]["greeks"]["delta"])
        self.assertNotIn("secret-should-never-appear", str(result))

    def test_first_order_models_use_the_selected_route_and_keep_iv(self):
        for model, endpoint in (
            ("standard", "option_snapshot_greeks_first_order"),
            ("binomial", "option_snapshot_binomial_greeks_first_order"),
        ):
            with self.subTest(model=model):
                client = FakeClient()
                calls = []

                def snapshot(**params):
                    calls.append(params)
                    return Frame([row(delta=.52, theta="-.2", implied_vol=.25)])

                setattr(client, endpoint, snapshot)
                args = {} if model == "standard" else {"greeks_model": model}
                result = OptionsService(client).chain(
                    "QQQ", expiration="2026-11-06", strike="500.5", right="call", **args)
                self.assertEqual(calls, [client.calls[0]])
                self.assertEqual(result["source_status"]["greeks"], {
                    "status": "ok", "row_count": 1, "model": model, "endpoint": endpoint})
                item = result["rows"][0]
                self.assertEqual(item["greeks"]["delta"], .52)
                self.assertEqual(item["greeks"]["theta"], -.2)
                self.assertIsNone(item["greeks"]["gamma"])
                self.assertEqual(item["implied_vol"], .25)

    def test_zero_dte_uses_date_instead_of_sdk_zero_filter(self):
        client = FakeClient()
        OptionsService(client).chain("QQQ", max_dte=0, include_greeks=False, include_open_interest=False)
        self.assertNotEqual(client.calls[0]["expiration"], "*")
        self.assertNotIn("max_dte", client.calls[0])

    def test_default_bounded_and_explicit_expiration_not_dte_limited(self):
        client = FakeClient()
        service = OptionsService(client)
        service.chain("QQQ", include_greeks=False, include_open_interest=False)
        self.assertEqual(client.calls[-1]["max_dte"], 7)
        service.chain("QQQ", expiration="2027-01-15", include_greeks=False, include_open_interest=False)
        self.assertNotIn("max_dte", client.calls[-1])

    def test_sensitive_errors_are_not_returned(self):
        self.assertNotIn("api-key-value", str(provider_error(RuntimeError("api-key-value"))))

    @patch("data.time.sleep")
    def test_expirations_recover_from_internal_error_with_same_request(self, sleep):
        error = ProviderRPCError(grpc.StatusCode.INTERNAL)
        client = Mock()
        client.option_list_expirations.side_effect = [error, error, Frame([
            {"expiration": "2026-11-13"}, {"expiration": "2026-11-09"},
            {"expiration": "2026-11-09"}, {"expiration": "2026-11-06"},
        ])]
        result = OptionsService(client).expirations("QQQ", "2026-11-07", "2026-11-13")
        self.assertEqual(result, {"symbol": "QQQ", "expirations": ["2026-11-09", "2026-11-13"]})
        self.assertEqual(client.option_list_expirations.call_args_list, [call(symbol="QQQ")] * 3)
        self.assertEqual(sleep.call_args_list, [call(.25), call(.5)])

    @patch("data.time.sleep")
    def test_expirations_stop_after_three_transient_failures(self, sleep):
        error = ProviderRPCError(grpc.StatusCode.INTERNAL)
        client = Mock()
        client.option_list_expirations.side_effect = error
        with self.assertRaises(ProviderRPCError) as caught:
            OptionsService(client).expirations("QQQ")
        self.assertIs(caught.exception, error)
        self.assertEqual(client.option_list_expirations.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("internal server error", provider_error(error)["message"])
        self.assertNotIn("secret-provider-detail", str(provider_error(error)))

    @patch("data.time.sleep")
    def test_permanent_failures_and_empty_results_are_not_retried(self, sleep):
        for failure in (ProviderRPCError(grpc.StatusCode.PERMISSION_DENIED),
                        ProviderRPCError(grpc.StatusCode.UNAUTHENTICATED),
                        ProviderRPCError(grpc.StatusCode.NOT_FOUND), ValueError("bad input")):
            with self.subTest(failure=failure):
                client = Mock()
                client.option_list_expirations.side_effect = failure
                with self.assertRaises(type(failure)):
                    OptionsService(client).expirations("QQQ")
                client.option_list_expirations.assert_called_once_with(symbol="QQQ")
        client = Mock()
        client.option_list_expirations.return_value = Frame([])
        self.assertEqual(OptionsService(client).expirations("QQQ")["expirations"], [])
        client.option_list_expirations.assert_called_once_with(symbol="QQQ")
        sleep.assert_not_called()

    @patch("data.time.sleep")
    def test_chain_retries_materialization_without_refetching_successful_sources(self, sleep):
        client = FakeClient()
        failed_frame = Mock()
        failed_frame.to_dicts.side_effect = ProviderRPCError(grpc.StatusCode.INTERNAL)
        client.option_snapshot_binomial_greeks_first_order = Mock(side_effect=[
            failed_frame, Frame([row(delta=.52, implied_vol=.25)]),
        ])
        result = OptionsService(client).chain("QQQ", greeks_model="binomial", max_dte=5, strike_range=1)
        self.assertFalse(result["partial"])
        self.assertEqual(result["rows"][0]["greeks"]["delta"], .52)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.option_snapshot_binomial_greeks_first_order.call_args_list,
                         [call(**client.calls[0])] * 2)

    @patch("data.time.sleep")
    def test_exhausted_chain_source_keeps_other_sources_and_safe_error(self, sleep):
        client = FakeClient()
        client.option_snapshot_greeks_first_order = Mock(side_effect=ProviderRPCError(grpc.StatusCode.INTERNAL))
        result = OptionsService(client).chain("QQQ")
        self.assertEqual(client.option_snapshot_greeks_first_order.call_count, 3)
        self.assertTrue(result["partial"])
        self.assertEqual(result["source_status"]["greeks"]["code"], "INTERNAL")
        self.assertEqual(result["rows"][0]["bid"], 3)
        self.assertEqual(result["rows"][0]["open_interest"], 123)
        self.assertNotIn("secret-provider-detail", str(result))

    def test_symbol_and_schema_bounds(self):
        self.assertEqual(symbol_checked(" qqq "), "QQQ")
        for invalid in ("*", "QQQ,AAPL", "QQQ;echo x"):
            with self.assertRaises(ValueError):
                symbol_checked(invalid)
        tools = asyncio.run(mcp.list_tools())
        self.assertEqual(len(tools), 4)
        tool = next(t for t in tools if t.name == "get_option_chain")
        self.assertEqual(tool.inputSchema["properties"]["strike_range"]["minimum"], 1)


if __name__ == "__main__":
    unittest.main()
