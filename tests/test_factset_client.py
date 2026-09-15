"""FactSet client tests.

No network and no credentials: the token provider and the requests session are
both injected, so every code path below runs against a fake transport. The live
OAuth exchange against auth.factset.com is not covered here — it needs real
credentials and outbound access to FactSet.
"""

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from factset_client import (  # noqa: E402
    FactSetAPIError,
    FactSetAuthError,
    FactSetClient,
    FactSetConfigError,
    asx_symbol,
    asx_symbols,
    find_config_path,
    load_config,
)

# A structurally complete JWK. The values are placeholders — nothing here signs
# anything, the tests only exercise validation.
FAKE_JWK = {key: "x" for key in
            ("kty", "alg", "use", "kid", "n", "e", "d", "p", "q", "dp", "dq", "qi")}
FAKE_CONFIG = {"name": "test app", "clientId": "test-client",
               "clientAuthType": "Confidential", "jwk": FAKE_JWK}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self.content = b"x" if payload is not None or text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeSession:
    """Records requests and replays a queued list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.proxies = {}
        self.verify = True
        self.closed = False

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params,
                           "body": json, "headers": headers, "timeout": timeout})
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def client(responses, **kwargs):
    """A client wired to a fake session, a fake token and a no-op sleep."""
    session = FakeSession(responses)
    kwargs.setdefault("max_retries", 0)
    fs = FactSetClient(
        token_provider=lambda: "test-token",
        session=session,
        sleep=lambda _seconds: None,
        **kwargs,
    )
    return fs, session


def price_rows(*symbols):
    return {"data": [{"requestId": s, "fsymId": f"{s}-S", "date": "2024-03-01",
                      "price": 1.0, "volume": 100} for s in symbols]}


class TestSymbols(unittest.TestCase):
    def test_plain_code_gets_au_region(self):
        self.assertEqual(asx_symbol("SXE"), "SXE-AU")

    def test_code_is_upcased_and_stripped(self):
        self.assertEqual(asx_symbol("  slc "), "SLC-AU")

    def test_qualified_symbol_is_left_alone(self):
        self.assertEqual(asx_symbol("BHP-AU"), "BHP-AU")
        self.assertEqual(asx_symbol("AAPL-US"), "AAPL-US")

    def test_list_preserves_order(self):
        self.assertEqual(asx_symbols(["IPG", "VEA"]), ["IPG-AU", "VEA-AU"])

    def test_empty_code_rejected(self):
        with self.assertRaises(ValueError):
            asx_symbol("   ")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "factset.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload) if not isinstance(payload, str)
                             else payload, encoding="utf-8")
        return self.path

    def test_valid_config_loads(self):
        config = load_config(self.write(FAKE_CONFIG))
        self.assertEqual(config["clientId"], "test-client")

    def test_missing_file_is_reported_with_its_path(self):
        missing = Path(self.tmp.name) / "nope.json"
        with self.assertRaises(FactSetConfigError) as caught:
            find_config_path(missing)
        self.assertIn("nope.json", str(caught.exception))

    def test_malformed_json_is_reported(self):
        with self.assertRaises(FactSetConfigError) as caught:
            load_config(self.write("{not json"))
        self.assertIn("not valid JSON", str(caught.exception))

    def test_missing_client_id_is_reported(self):
        payload = {k: v for k, v in FAKE_CONFIG.items() if k != "clientId"}
        with self.assertRaises(FactSetConfigError) as caught:
            load_config(self.write(payload))
        self.assertIn("clientId", str(caught.exception))

    def test_missing_jwk_block_is_reported(self):
        payload = {k: v for k, v in FAKE_CONFIG.items() if k != "jwk"}
        with self.assertRaises(FactSetConfigError) as caught:
            load_config(self.write(payload))
        self.assertIn("jwk", str(caught.exception))

    def test_incomplete_jwk_names_the_missing_keys(self):
        partial = {k: v for k, v in FAKE_JWK.items() if k not in ("q", "dq")}
        with self.assertRaises(FactSetConfigError) as caught:
            load_config(self.write({**FAKE_CONFIG, "jwk": partial}))
        message = str(caught.exception)
        self.assertIn("q", message)
        self.assertIn("dq", message)

    def test_env_var_is_used_when_set(self):
        path = self.write(FAKE_CONFIG)
        with unittest.mock.patch.dict("os.environ",
                                      {"FACTSET_CONFIG_PATH": str(path)}):
            self.assertEqual(find_config_path(), path)

    def test_env_var_pointing_nowhere_is_reported(self):
        with unittest.mock.patch.dict("os.environ",
                                      {"FACTSET_CONFIG_PATH": "/no/such/file.json"}):
            with self.assertRaises(FactSetConfigError) as caught:
                find_config_path()
        self.assertIn("FACTSET_CONFIG_PATH", str(caught.exception))


class TestTransport(unittest.TestCase):
    def test_bearer_token_is_sent(self):
        fs, session = client([FakeResponse(200, {"data": []})])
        fs.get("/some/path")
        self.assertEqual(session.calls[0]["headers"]["Authorization"],
                         "Bearer test-token")

    def test_none_params_are_dropped(self):
        fs, session = client([FakeResponse(200, {"data": []})])
        fs.get("/some/path", ids="BHP-AU", currency=None)
        self.assertEqual(session.calls[0]["params"], {"ids": "BHP-AU"})

    def test_204_returns_empty_dict(self):
        fs, _ = client([FakeResponse(204)])
        self.assertEqual(fs.get("/some/path"), {})

    def test_401_raises_auth_error_not_api_error(self):
        fs, _ = client([FakeResponse(401, {"errors": [{"title": "Unauthorized"}]})])
        with self.assertRaises(FactSetAuthError) as caught:
            fs.get("/some/path")
        self.assertIn("entitled", str(caught.exception))

    def test_403_raises_auth_error(self):
        fs, _ = client([FakeResponse(403, {"detail": "not entitled"})])
        with self.assertRaises(FactSetAuthError):
            fs.get("/some/path")

    def test_auth_failure_is_not_retried(self):
        fs, session = client([FakeResponse(401, {"detail": "no"})], max_retries=3)
        with self.assertRaises(FactSetAuthError):
            fs.get("/some/path")
        self.assertEqual(len(session.calls), 1)

    def test_400_surfaces_the_factset_error_detail(self):
        fs, _ = client([FakeResponse(
            400, {"errors": [{"title": "Bad Request",
                              "detail": "Invalid metric FF_NOPE"}]})])
        with self.assertRaises(FactSetAPIError) as caught:
            fs.get("/some/path")
        self.assertIn("Invalid metric FF_NOPE", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 400)

    def test_request_key_is_captured_for_support(self):
        fs, _ = client([FakeResponse(
            500, {"detail": "boom"},
            headers={"x-datadirect-request-key": "abc-123"})])
        with self.assertRaises(FactSetAPIError) as caught:
            fs.get("/some/path")
        self.assertEqual(caught.exception.request_key, "abc-123")
        self.assertIn("abc-123", str(caught.exception))

    def test_429_is_retried_then_succeeds(self):
        fs, session = client(
            [FakeResponse(429, {"detail": "slow down"}, {"Retry-After": "0"}),
             FakeResponse(200, {"data": [{"price": 1.0}]})],
            max_retries=2,
        )
        payload = fs.get("/some/path")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(payload["data"][0]["price"], 1.0)

    def test_retries_are_bounded_and_then_raise(self):
        fs, session = client(
            [FakeResponse(503, {"detail": "down"}) for _ in range(3)],
            max_retries=2,
        )
        with self.assertRaises(FactSetAPIError):
            fs.get("/some/path")
        self.assertEqual(len(session.calls), 3)

    def test_non_json_error_body_still_produces_a_message(self):
        fs, _ = client([FakeResponse(502, None, text="<html>gateway</html>")])
        with self.assertRaises(FactSetAPIError) as caught:
            fs.get("/some/path")
        self.assertIn("gateway", str(caught.exception))

    def test_context_manager_closes_only_owned_sessions(self):
        session = FakeSession([])
        with FactSetClient(token_provider=lambda: "t", session=session):
            pass
        self.assertFalse(session.closed)


class TestEndpoints(unittest.TestCase):
    def test_prices_posts_expected_body(self):
        fs, session = client([FakeResponse(200, price_rows("BHP-AU"))])
        fs.prices("BHP-AU", start_date="2024-01-01", end_date="2024-03-01")
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith("/factset-global-prices/v1/prices"))
        data = call["body"]["data"]
        self.assertEqual(data["ids"], ["BHP-AU"])
        self.assertEqual(data["startDate"], "2024-01-01")
        self.assertEqual(data["frequency"], "D")
        self.assertEqual(data["adjust"], "SPLIT")

    def test_optional_price_args_are_omitted_not_nulled(self):
        fs, session = client([FakeResponse(200, price_rows("BHP-AU"))])
        fs.prices("BHP-AU", start_date="2024-01-01")
        data = session.calls[0]["body"]["data"]
        self.assertNotIn("endDate", data)
        self.assertNotIn("currency", data)
        self.assertNotIn("fields", data)

    def test_ids_are_batched_at_fifty(self):
        ids = [f"T{n:03d}-AU" for n in range(120)]
        fs, session = client([FakeResponse(200, price_rows(*ids[0:50])),
                              FakeResponse(200, price_rows(*ids[50:100])),
                              FakeResponse(200, price_rows(*ids[100:120]))])
        frame = fs.prices(ids, start_date="2024-01-01")
        self.assertEqual(len(session.calls), 3)
        sizes = [len(call["body"]["data"]["ids"]) for call in session.calls]
        self.assertEqual(sizes, [50, 50, 20])
        self.assertEqual(len(frame), 120)

    def test_batches_are_concatenated_with_a_clean_index(self):
        ids = [f"T{n:03d}-AU" for n in range(60)]
        fs, _ = client([FakeResponse(200, price_rows(*ids[0:50])),
                        FakeResponse(200, price_rows(*ids[50:60]))])
        frame = fs.prices(ids, start_date="2024-01-01")
        self.assertEqual(list(frame.index), list(range(60)))

    def test_empty_data_gives_an_empty_frame_not_an_error(self):
        fs, _ = client([FakeResponse(200, {"data": []})])
        frame = fs.prices("BHP-AU", start_date="2024-01-01")
        self.assertIsInstance(frame, pd.DataFrame)
        self.assertTrue(frame.empty)

    def test_a_batch_with_no_rows_does_not_sink_the_others(self):
        ids = [f"T{n:03d}-AU" for n in range(60)]
        fs, _ = client([FakeResponse(200, {"data": []}),
                        FakeResponse(200, price_rows(*ids[50:60]))])
        frame = fs.prices(ids, start_date="2024-01-01")
        self.assertEqual(len(frame), 10)

    def test_single_string_id_is_accepted(self):
        fs, session = client([FakeResponse(200, price_rows("SXE-AU"))])
        fs.prices("SXE-AU", start_date="2024-01-01")
        self.assertEqual(session.calls[0]["body"]["data"]["ids"], ["SXE-AU"])

    def test_empty_id_list_is_rejected_before_any_request(self):
        fs, session = client([])
        with self.assertRaises(ValueError):
            fs.prices([], start_date="2024-01-01")
        self.assertEqual(session.calls, [])

    def test_returns_endpoint(self):
        fs, session = client([FakeResponse(200, {"data": [{"totalReturn": 1.2}]})])
        fs.returns("BHP-AU", start_date="2024-01-01", end_date="2024-03-01")
        call = session.calls[0]
        self.assertTrue(call["url"].endswith("/factset-global-prices/v1/returns"))
        self.assertEqual(call["body"]["data"]["endDate"], "2024-03-01")

    def test_corporate_actions_defaults_to_all_events(self):
        fs, session = client([FakeResponse(200, {"data": []})])
        fs.corporate_actions("BHP-AU")
        data = session.calls[0]["body"]["data"]
        self.assertEqual(data["eventCategory"], "ALL")

    def test_shares_outstanding_endpoint(self):
        fs, session = client([FakeResponse(200, {"data": [{"shares": 5.0}]})])
        fs.shares_outstanding("BHP-AU")
        self.assertTrue(session.calls[0]["url"]
                        .endswith("/factset-global-prices/v1/shares-outstanding"))

    def test_fundamentals_sends_metrics_and_periodicity(self):
        fs, session = client([FakeResponse(200, {"data": [{"value": 1.0}]})])
        fs.fundamentals("BHP-AU", metrics=["FF_SALES", "FF_NET_INC"],
                        periodicity="QTR")
        data = session.calls[0]["body"]["data"]
        self.assertEqual(data["metrics"], ["FF_SALES", "FF_NET_INC"])
        self.assertEqual(data["periodicity"], "QTR")

    def test_fundamentals_requires_at_least_one_metric(self):
        fs, session = client([])
        with self.assertRaises(ValueError):
            fs.fundamentals("BHP-AU", metrics=[])
        self.assertEqual(session.calls, [])

    def test_metrics_catalogue_is_a_get(self):
        fs, session = client([FakeResponse(200, {"data": [{"metric": "FF_SALES"}]})])
        frame = fs.fundamentals_metrics(category="INCOME_STATEMENT")
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertEqual(session.calls[0]["params"],
                         {"category": "INCOME_STATEMENT"})
        self.assertEqual(frame.iloc[0]["metric"], "FF_SALES")

    def test_nested_response_fields_are_flattened(self):
        fs, _ = client([FakeResponse(200, {"data": [
            {"requestId": "BHP-AU", "price": {"close": 44.1, "currency": "AUD"}}]})])
        frame = fs.prices("BHP-AU", start_date="2024-01-01")
        self.assertIn("price.close", frame.columns)
        self.assertEqual(frame.iloc[0]["price.close"], 44.1)


if __name__ == "__main__":
    unittest.main()
