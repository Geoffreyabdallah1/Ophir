"""Signal-engine tests against synthetic price series.

The yfinance download path is not covered here — it needs live network access.
Everything downstream of the raw OHLCV frame is.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asx_breakout_scan import (  # noqa: E402
    ScanConfig,
    compute_signals,
    load_universe,
    parse_market_cap,
    rsi,
    scan,
    sector_strength,
)

CFG = ScanConfig()


def series(closes, volumes=None, highs=None, lows=None) -> pd.DataFrame:
    """Build an OHLCV frame from a close path, with sane synthetic high/low."""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    index = pd.bdate_range("2023-01-02", periods=n)
    volumes = np.full(n, 1_000_000.0) if volumes is None else np.asarray(volumes, dtype=float)
    highs = closes * 1.005 if highs is None else np.asarray(highs, dtype=float)
    lows = closes * 0.995 if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=index,
    )


class TestBreakouts(unittest.TestCase):
    def test_confirmed_breakout(self):
        closes = [10.0] * 200 + [12.0]
        volumes = [1_000_000.0] * 200 + [4_000_000.0]
        row = compute_signals(series(closes, volumes), CFG)
        self.assertTrue(row["Breakout"])
        self.assertFalse(row["Breakdown"])
        self.assertTrue(row["Confirmed"])
        self.assertAlmostEqual(row["VolumeRatio"], 4.0, places=6)

    def test_unconfirmed_breakout(self):
        closes = [10.0] * 200 + [12.0]
        volumes = [1_000_000.0] * 201
        row = compute_signals(series(closes, volumes), CFG)
        self.assertTrue(row["Breakout"])
        self.assertFalse(row["Confirmed"])
        self.assertAlmostEqual(row["VolumeRatio"], 1.0, places=6)

    def test_breakdown(self):
        closes = [10.0] * 200 + [8.0]
        volumes = [1_000_000.0] * 200 + [3_000_000.0]
        row = compute_signals(series(closes, volumes), CFG)
        self.assertTrue(row["Breakdown"])
        self.assertFalse(row["Breakout"])
        self.assertTrue(row["Confirmed"])

    def test_inside_the_channel_is_no_signal(self):
        # The channel is built from intraday highs/lows, so a close of 10.02 sits
        # inside a prior-60-session high of 10.05 and must not signal.
        closes = [10.0] * 200 + [10.02]
        row = compute_signals(series(closes), CFG)
        self.assertFalse(row["Breakout"])
        self.assertFalse(row["Breakdown"])

    def test_channel_excludes_today(self):
        # Today must be compared to the prior 60 sessions, not to a window that
        # already contains today's own high.
        closes = [10.0] * 200 + [12.0]
        row = compute_signals(series(closes), CFG)
        self.assertAlmostEqual(row["Donchian60High"], 10.0 * 1.005, places=6)

    def test_short_history_is_skipped(self):
        self.assertIsNone(compute_signals(series([10.0] * 20), CFG))


class TestFiftyTwoWeek(unittest.TestCase):
    def test_new_low_uses_intraday_low(self):
        # Close drifts down but today's low still sits above the old low: not a
        # new 52-week low. A percentage band off the close would misfire here.
        closes = [10.0] * 100 + [9.0] * 100 + [9.02]
        lows = [9.5] * 100 + [8.5] * 100 + [8.6]
        row = compute_signals(series(closes, lows=lows), CFG)
        self.assertFalse(row["New 52wk Low"])

    def test_genuine_new_low(self):
        closes = [10.0] * 100 + [9.0] * 100 + [8.0]
        lows = [9.5] * 100 + [8.5] * 100 + [7.9]
        row = compute_signals(series(closes, lows=lows), CFG)
        self.assertTrue(row["New 52wk Low"])

    def test_genuine_new_high(self):
        closes = list(np.linspace(10.0, 20.0, 260))
        row = compute_signals(series(closes), CFG)
        self.assertTrue(row["New 52wk High"])
        self.assertAlmostEqual(row["Pct from 52wk High"], -0.4975, places=3)


class TestMovingAverages(unittest.TestCase):
    def test_golden_cross(self):
        # Long decline then a sharp sustained rally pulls the 50DMA up through
        # the 200DMA within the last few sessions.
        closes = list(np.linspace(20.0, 10.0, 260)) + list(np.linspace(10.0, 26.0, 120))
        row = compute_signals(series(closes), CFG)
        self.assertTrue(row["GoldenCross"] or row["Above 200DMA"])
        self.assertTrue(row["Above 50DMA"])

    def test_death_cross_flag_is_exclusive(self):
        closes = [10.0] * 300
        row = compute_signals(series(closes), CFG)
        self.assertFalse(row["GoldenCross"])
        self.assertFalse(row["DeathCross"])

    def test_pct_vs_ma(self):
        closes = [10.0] * 200 + [11.0]
        row = compute_signals(series(closes), CFG)
        # 20DMA sits at (19 * 10 + 11) / 20 = 10.05
        self.assertAlmostEqual(row["Pct vs 20DMA"], (11.0 / 10.05 - 1) * 100, places=6)
        self.assertTrue(row["Above 200DMA"])


class TestRsi(unittest.TestCase):
    def test_monotonic_rise_pins_at_100(self):
        values = rsi(pd.Series(np.linspace(10.0, 30.0, 120)))
        self.assertAlmostEqual(values.iloc[-1], 100.0, places=6)

    def test_monotonic_fall_pins_at_zero(self):
        values = rsi(pd.Series(np.linspace(30.0, 10.0, 120)))
        self.assertAlmostEqual(values.iloc[-1], 0.0, places=6)

    def test_flat_series_is_fifty(self):
        values = rsi(pd.Series([10.0] * 120))
        self.assertAlmostEqual(values.iloc[-1], 50.0, places=6)

    def test_bounds(self):
        rng = np.random.default_rng(7)
        path = 10 * np.exp(np.cumsum(rng.normal(0, 0.02, 500)))
        values = rsi(pd.Series(path)).dropna()
        self.assertTrue(((values >= 0) & (values <= 100)).all())


class TestTurnover(unittest.TestCase):
    def test_adv20(self):
        closes = [5.0] * 100
        volumes = [200_000.0] * 100
        row = compute_signals(series(closes, volumes), CFG)
        self.assertAlmostEqual(row["ADV20 (A$)"], 1_000_000.0, places=6)

    def test_liquidity_gate(self):
        universe = pd.DataFrame({
            "Ticker": ["AAA", "BBB"],
            "Name": ["Liquid Co", "Shell Co"],
            "Sector": ["Materials", "Materials"],
            "MarketCap": [5e8, 1e7],
        })
        prices = {
            "AAA": series([10.0] * 200 + [12.0], [500_000.0] * 201),
            "BBB": series([1.0] * 200 + [1.4], [1_000.0] * 201),
        }
        results, skipped = scan(universe, prices, ScanConfig(min_adv=250_000))
        self.assertTrue(skipped.empty)
        liquid = dict(zip(results["Ticker"], results["Liquid"]))
        self.assertTrue(liquid["AAA"])
        self.assertFalse(liquid["BBB"])
        self.assertTrue(results.set_index("Ticker").at["BBB", "Breakout"])

    def test_missing_history_is_reported(self):
        universe = pd.DataFrame({
            "Ticker": ["AAA", "ZZZ"], "Name": ["A", "Z"],
            "Sector": ["Energy", "Energy"], "MarketCap": [1e8, 1e8],
        })
        results, skipped = scan(universe, {"AAA": series([10.0] * 100)}, CFG)
        self.assertEqual(len(results), 1)
        self.assertEqual(skipped.iloc[0]["Ticker"], "ZZZ")
        self.assertEqual(skipped.iloc[0]["Reason"], "No price history")


class TestUniverse(unittest.TestCase):
    def _write(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        handle.write(text)
        handle.close()
        return Path(handle.name)

    def test_top_n_exclusion_and_ordinary_filter(self):
        path = self._write(
            "ASX code,Company name,GICS industry group,Market Cap\n"
            "BHP,BHP Group,Materials,200000000000\n"
            "CBA,Commonwealth Bank,Banks,180000000000\n"
            "XYZ,Small Co,Materials,150000000\n"
            "ABC,Tiny Co,Energy,40000000\n"
            "XYZOA,Small Co Options,Materials,0\n"
        )
        universe, excluded = load_universe(path, exclude_top=2)
        self.assertEqual(sorted(universe["Ticker"]), ["ABC", "XYZ"])
        reasons = dict(zip(excluded["Ticker"], excluded["Reason"]))
        self.assertEqual(reasons["XYZOA"], "Not an ordinary line (option/warrant/note)")
        self.assertEqual(reasons["BHP"], "Top 2 by market cap")
        self.assertEqual(reasons["CBA"], "Top 2 by market cap")

    def test_column_autodetection_and_dedupe(self):
        path = self._write(
            "Symbol,Description,Sector,MarketCap\n"
            "AAA,Alpha,Energy,1.5B\n"
            "AAA,Alpha duplicate,Energy,1.5B\n"
            "BBB,Beta,,250M\n"
        )
        universe, _ = load_universe(path, exclude_top=0)
        self.assertEqual(sorted(universe["Ticker"]), ["AAA", "BBB"])
        self.assertEqual(universe.set_index("Ticker").at["AAA", "MarketCap"], 1.5e9)
        self.assertEqual(universe.set_index("Ticker").at["BBB", "Sector"], "Unclassified")

    def test_missing_market_cap_survives_top_n(self):
        path = self._write(
            "Code,Name,Sector,Market Cap\n"
            "BHP,BHP,Materials,200000000000\n"
            "NEW,Newly listed,Materials,\n"
        )
        universe, excluded = load_universe(path, exclude_top=1)
        self.assertEqual(universe["Ticker"].tolist(), ["NEW"])
        self.assertEqual(excluded["Ticker"].tolist(), ["BHP"])

    def test_missing_ticker_column_raises(self):
        path = self._write("Foo,Bar\n1,2\n")
        with self.assertRaises(ValueError):
            load_universe(path)

    def test_parse_market_cap(self):
        self.assertEqual(parse_market_cap("$1,234"), 1234.0)
        self.assertEqual(parse_market_cap("1.5B"), 1.5e9)
        self.assertEqual(parse_market_cap("250m"), 2.5e8)
        self.assertEqual(parse_market_cap(4200), 4200.0)
        self.assertTrue(np.isnan(parse_market_cap("")))
        self.assertTrue(np.isnan(parse_market_cap("n/a")))


class TestSectorStrength(unittest.TestCase):
    def test_breadth_columns(self):
        universe = pd.DataFrame({
            "Ticker": ["AAA", "BBB", "CCC"],
            "Name": ["A", "B", "C"],
            "Sector": ["Materials", "Materials", "Energy"],
            "MarketCap": [1e8, 1e8, 1e8],
        })
        prices = {
            "AAA": series([10.0] * 200 + [12.0], [500_000.0] * 201),
            "BBB": series([10.0] * 200 + [8.0], [500_000.0] * 201),
            "CCC": series([10.0] * 201, [500_000.0] * 201),
        }
        results, _ = scan(universe, prices, CFG)
        table = sector_strength(results).set_index("Sector")
        self.assertEqual(table.at["Materials", "Count"], 2)
        self.assertEqual(table.at["Materials", "Breakouts"], 1)
        self.assertEqual(table.at["Materials", "Breakdowns"], 1)
        self.assertEqual(table.at["Materials", "Net Breadth"], 0)
        self.assertEqual(table.at["Energy", "Breakouts"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
