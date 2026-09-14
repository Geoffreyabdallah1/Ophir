#!/usr/bin/env python3
"""ASX ex-100 breakout scanner.

Scans the ASX listed universe minus the largest N names by market capitalisation
for Donchian channel breakouts and breakdowns, moving-average structure, RSI,
relative strength and 52-week extremes, then writes a dated Excel workbook.

Typical use:

    pip install -r requirements.txt
    python asx_breakout_scan.py --universe asx_universe.csv

The universe file is any CSV carrying a ticker column plus (ideally) company
name, sector and market cap. asxlistedcompanies.com publishes one in exactly
that shape. Column names are auto-detected, options/warrants/notes are stripped
back to ordinary lines, and the largest ``--exclude-top`` names by market cap are
dropped, so "ex-100" is computed from the data rather than a stale index list.

Price history comes from Yahoo Finance via yfinance. Pass ``--cache-prices`` on a
live run and ``--prices`` on later runs to re-scan the same history offline.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Universe loading
# --------------------------------------------------------------------------- #

TICKER_ALIASES = ("ticker", "code", "asx code", "asx_code", "symbol", "asx",
                  "security code", "asx ticker")
NAME_ALIASES = ("company name", "company", "name", "security name", "description")
SECTOR_ALIASES = ("gics sector", "sector", "gics industry group", "industry group",
                  "industry", "gics")
MCAP_ALIASES = ("market cap", "marketcap", "market capitalisation",
                "market capitalization", "market_cap", "market cap (aud)",
                "market cap aud")

# ASX ordinary lines are exactly three characters. Options, rights, warrants and
# notes carry a four to six character code (XYZO, XYZOA, XYZAI, ...).
ORDINARY_RE = re.compile(r"^[A-Z0-9]{3}$")

_SUFFIX_MULTIPLIERS = {"K": 1e3, "M": 1e6, "B": 1e9, "BN": 1e9, "T": 1e12}


def _normalise_header(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip().lower())


def _find_column(columns: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    """Resolve a logical column to a real header: exact alias first, then substring."""
    for alias in aliases:
        if alias in columns:
            return columns[alias]
    for alias in aliases:
        for norm, original in columns.items():
            if alias in norm:
                return original
    return None


def parse_market_cap(value) -> float:
    """Parse '$1,234.5M', '1.2bn', 1234567 and friends into a float. NaN if unusable."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return float("nan")
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("A$", "")
    if not text or text in {"-", "n/a", "N/A", "NA", "None"}:
        return float("nan")
    match = re.match(r"^(-?\d*\.?\d+)\s*([A-Za-z]*)$", text)
    if not match:
        return float("nan")
    number = float(match.group(1))
    suffix = match.group(2).upper()
    if not suffix:
        return number
    return number * _SUFFIX_MULTIPLIERS.get(suffix, float("nan"))


def load_universe(source: str | Path, exclude_top: int = 100) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load a listed-companies CSV (local path or URL) into (scan set, excluded set).

    Returns two frames with columns Ticker/Name/Sector/MarketCap; the excluded
    frame carries an extra Reason column.
    """
    raw = pd.read_csv(source, dtype=str)
    columns = {_normalise_header(c): c for c in raw.columns}

    ticker_col = _find_column(columns, TICKER_ALIASES)
    if ticker_col is None:
        raise ValueError(
            f"No ticker column found in {source}. Looked for one of: "
            + ", ".join(TICKER_ALIASES)
        )
    name_col = _find_column(columns, NAME_ALIASES)
    sector_col = _find_column(columns, SECTOR_ALIASES)
    mcap_col = _find_column(columns, MCAP_ALIASES)

    def text(column: str | None, default: str = "") -> pd.Series:
        """A blank-filled string column; missing source columns fall back to a default."""
        if column is None:
            return pd.Series([default] * len(raw), index=raw.index, dtype="object")
        return raw[column].fillna("").astype(str).str.strip().replace({"nan": ""})

    frame = pd.DataFrame({
        "Ticker": text(ticker_col).str.upper(),
        "Name": text(name_col),
        "Sector": text(sector_col, "Unclassified"),
        "MarketCap": raw[mcap_col].map(parse_market_cap) if mcap_col else float("nan"),
    })
    frame = frame[frame["Ticker"].str.len() > 0]
    frame["Sector"] = frame["Sector"].replace({"": "Unclassified"})
    frame = frame.drop_duplicates(subset="Ticker", keep="first").reset_index(drop=True)

    excluded: list[pd.DataFrame] = []

    non_ordinary = frame[~frame["Ticker"].str.match(ORDINARY_RE)].copy()
    if not non_ordinary.empty:
        non_ordinary["Reason"] = "Not an ordinary line (option/warrant/note)"
        excluded.append(non_ordinary)
    frame = frame[frame["Ticker"].str.match(ORDINARY_RE)].copy()

    if exclude_top > 0 and not frame.empty:
        # Names without a market cap cannot be ranked; treat them as small so a
        # missing value never knocks a genuine small-cap out of the scan.
        ranked = frame.sort_values("MarketCap", ascending=False, na_position="last")
        top = ranked.head(exclude_top).copy()
        top = top[top["MarketCap"].notna()]
        if not top.empty:
            top["Reason"] = f"Top {exclude_top} by market cap"
            excluded.append(top)
            frame = frame[~frame["Ticker"].isin(top["Ticker"])].copy()

    excluded_frame = (
        pd.concat(excluded, ignore_index=True)
        if excluded
        else pd.DataFrame(columns=["Ticker", "Name", "Sector", "MarketCap", "Reason"])
    )
    return frame.reset_index(drop=True), excluded_frame


# --------------------------------------------------------------------------- #
# Price history
# --------------------------------------------------------------------------- #

PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]


def to_yahoo(ticker: str) -> str:
    return f"{ticker}.AX"


def _extract(raw: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """Pull one ticker's block out of a yfinance frame, whichever way it grouped.

    group_by="ticker" puts the symbol on level 0 and the field on level 1;
    the column-grouped default is the other way round, and which one comes back
    has varied across yfinance releases. Find the symbol on whichever level holds it.
    """
    if not isinstance(raw.columns, pd.MultiIndex):
        return raw
    for level in range(raw.columns.nlevels):
        if symbol in raw.columns.get_level_values(level):
            return raw.xs(symbol, axis=1, level=level)
    return None


def _tidy(frame: pd.DataFrame) -> pd.DataFrame | None:
    """Coerce a yfinance frame into lower-case OHLCV indexed by date."""
    frame = frame.rename(columns={c: _normalise_header(c) for c in frame.columns})
    # With auto_adjust=True the close is already adjusted and an extra "Adj Close"
    # may still ride along. Renaming it blindly would leave two columns called
    # "close", and frame["close"] would then hand back a DataFrame, not a Series.
    if "adj close" in frame.columns:
        frame = (frame.drop(columns=["adj close"]) if "close" in frame.columns
                 else frame.rename(columns={"adj close": "close"}))
    frame = frame.loc[:, ~frame.columns.duplicated()]
    if not set(PRICE_COLUMNS).issubset(frame.columns):
        return None
    frame = frame[PRICE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    frame = frame.dropna(subset=["close"])
    if frame.empty:
        return None
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    return frame.sort_index()


def load_prices_csv(path: Path) -> dict[str, pd.DataFrame]:
    """Load a long-format price cache: ticker,date,open,high,low,close,volume."""
    raw = pd.read_csv(path)
    raw = raw.rename(columns={c: _normalise_header(c) for c in raw.columns})
    if "ticker" not in raw.columns or "date" not in raw.columns:
        raise ValueError(f"{path.name} needs at least 'ticker' and 'date' columns")
    raw["date"] = pd.to_datetime(raw["date"])
    out: dict[str, pd.DataFrame] = {}
    for ticker, group in raw.groupby("ticker"):
        tidy = _tidy(group.set_index("date").drop(columns=["ticker"]))
        if tidy is not None:
            out[str(ticker).upper()] = tidy
    return out


def save_prices_csv(prices: dict[str, pd.DataFrame], path: Path) -> None:
    frames = []
    for ticker, frame in prices.items():
        block = frame.copy()
        block.insert(0, "ticker", ticker)
        block.index.name = "date"
        frames.append(block.reset_index())
    if frames:
        pd.concat(frames, ignore_index=True).to_csv(path, index=False)


def download_prices(tickers: list[str], start: date, end: date, batch_size: int = 50,
                    pause: float = 1.0, retries: int = 3) -> dict[str, pd.DataFrame]:
    """Download daily OHLCV for ASX tickers, batched, with backoff on failure."""
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "yfinance is not installed. Run: pip install -r requirements.txt"
        ) from exc

    out: dict[str, pd.DataFrame] = {}
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    for n, batch in enumerate(batches, 1):
        symbols = [to_yahoo(t) for t in batch]
        raw = None
        for attempt in range(retries):
            try:
                raw = yf.download(
                    symbols, start=start, end=end, interval="1d",
                    auto_adjust=True, progress=False, threads=True, group_by="ticker",
                )
                break
            except Exception as exc:  # pragma: no cover - network dependent
                wait = pause * (2 ** attempt)
                print(f"  batch {n}/{len(batches)}: {exc} — retrying in {wait:.0f}s",
                      file=sys.stderr)
                time.sleep(wait)
        if raw is None or raw.empty:
            print(f"  batch {n}/{len(batches)}: no data returned", file=sys.stderr)
            continue

        for ticker, symbol in zip(batch, symbols):
            block = _extract(raw, symbol)
            if block is None:
                continue
            tidy = _tidy(block)
            if tidy is not None:
                out[ticker] = tidy
        print(f"  batch {n}/{len(batches)}: {len(out)} tickers with history",
              file=sys.stderr)
        if n < len(batches):
            time.sleep(pause)
    return out


# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #


@dataclass
class ScanConfig:
    donchian: int = 60
    volume_multiple: float = 2.0
    volume_window: int = 50
    cross_window: int = 5
    rsi_period: int = 14
    year_window: int = 252
    min_adv: float = 250_000.0


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    out = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss.replace(0.0, np.nan))
    # No down moves in the window: RSI is 100 if the stock rose, 50 if it was flat.
    flat = avg_loss.eq(0.0)
    out = out.mask(flat & avg_gain.gt(0.0), 100.0)
    out = out.mask(flat & avg_gain.eq(0.0), 50.0)
    return out


def _pct_change_over(close: pd.Series, periods: int) -> float:
    if len(close) <= periods:
        return float("nan")
    past = close.iloc[-(periods + 1)]
    if not past:
        return float("nan")
    return float((close.iloc[-1] / past - 1.0) * 100.0)


def _last(series: pd.Series) -> float:
    if series.empty:
        return float("nan")
    value = series.iloc[-1]
    return float(value) if pd.notna(value) else float("nan")


def compute_signals(frame: pd.DataFrame, cfg: ScanConfig) -> dict | None:
    """Compute the signal row for the latest bar. None if there is too little history."""
    if len(frame) < cfg.donchian + 2:
        return None

    close, high, low, volume = (frame["close"], frame["high"], frame["low"], frame["volume"])

    # Donchian channel from the N sessions *before* today, so today can break it.
    prior_high = high.rolling(cfg.donchian).max().shift(1)
    prior_low = low.rolling(cfg.donchian).min().shift(1)
    channel_high, channel_low = _last(prior_high), _last(prior_low)
    last_close = float(close.iloc[-1])
    breakout = bool(np.isfinite(channel_high) and last_close > channel_high)
    breakdown = bool(np.isfinite(channel_low) and last_close < channel_low)

    prior_avg_volume = _last(volume.rolling(cfg.volume_window).mean().shift(1))
    last_volume = float(volume.iloc[-1])
    volume_ratio = (last_volume / prior_avg_volume
                    if np.isfinite(prior_avg_volume) and prior_avg_volume > 0
                    else float("nan"))
    confirmed = bool(np.isfinite(volume_ratio) and volume_ratio >= cfg.volume_multiple)

    turnover = (close * volume).rolling(20).mean()
    adv20 = _last(turnover)

    ma20, ma50, ma200 = (close.rolling(w).mean() for w in (20, 50, 200))
    last_ma20, last_ma50, last_ma200 = _last(ma20), _last(ma50), _last(ma200)

    # Golden / death cross inside the last `cross_window` sessions.
    golden = death = False
    spread = (ma50 - ma200).dropna()
    if len(spread) > cfg.cross_window:
        window = spread.iloc[-(cfg.cross_window + 1):]
        signs = np.sign(window.to_numpy())
        golden = bool(np.any((signs[:-1] <= 0) & (signs[1:] > 0)))
        death = bool(np.any((signs[:-1] >= 0) & (signs[1:] < 0)))

    min_year = min(cfg.year_window, max(len(frame) - 1, 1))
    year_high = high.rolling(cfg.year_window, min_periods=min_year).max()
    year_low = low.rolling(cfg.year_window, min_periods=min_year).min()
    last_year_high, last_year_low = _last(year_high), _last(year_low)
    # Exact test against today's own high/low — a percentage band around the close
    # trips over the intraday range and misses genuine new extremes.
    new_high = bool(np.isfinite(last_year_high) and float(high.iloc[-1]) >= last_year_high)
    new_low = bool(np.isfinite(last_year_low) and float(low.iloc[-1]) <= last_year_low)

    def pct_from(level: float) -> float:
        return (last_close / level - 1.0) * 100.0 if np.isfinite(level) and level else float("nan")

    return {
        "Date": frame.index[-1].date(),
        "Close": last_close,
        "Volume": last_volume,
        "ADV20 (A$)": adv20,
        "Breakout": breakout,
        "Breakdown": breakdown,
        "Confirmed": confirmed,
        "VolumeRatio": volume_ratio,
        f"Donchian{cfg.donchian}High": channel_high,
        f"Donchian{cfg.donchian}Low": channel_low,
        "RSI14": _last(rsi(close, cfg.rsi_period)),
        "Pct vs 20DMA": pct_from(last_ma20),
        "Pct vs 50DMA": pct_from(last_ma50),
        "Pct vs 200DMA": pct_from(last_ma200),
        "Above 20DMA": bool(np.isfinite(last_ma20) and last_close > last_ma20),
        "Above 50DMA": bool(np.isfinite(last_ma50) and last_close > last_ma50),
        "Above 200DMA": bool(np.isfinite(last_ma200) and last_close > last_ma200),
        "GoldenCross": golden,
        "DeathCross": death,
        "Ret 5D %": _pct_change_over(close, 5),
        "Ret 21D %": _pct_change_over(close, 21),
        "Ret 63D %": _pct_change_over(close, 63),
        "Pct from 52wk High": pct_from(last_year_high),
        "Pct from 52wk Low": pct_from(last_year_low),
        "New 52wk High": new_high,
        "New 52wk Low": new_low,
    }


def scan(universe: pd.DataFrame, prices: dict[str, pd.DataFrame],
         cfg: ScanConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the signal pass. Returns (results, skipped)."""
    rows: list[dict] = []
    skipped: list[dict] = []
    meta = universe.set_index("Ticker")
    for ticker in universe["Ticker"]:
        frame = prices.get(ticker)
        if frame is None or frame.empty:
            skipped.append({"Ticker": ticker, "Reason": "No price history"})
            continue
        signals = compute_signals(frame, cfg)
        if signals is None:
            skipped.append({"Ticker": ticker,
                            "Reason": f"Fewer than {cfg.donchian + 2} sessions of history"})
            continue
        row = {
            "Ticker": ticker,
            "Name": meta.at[ticker, "Name"],
            "Sector": meta.at[ticker, "Sector"],
            "MarketCap": meta.at[ticker, "MarketCap"],
        }
        row.update(signals)
        row["Liquid"] = bool(np.isfinite(row["ADV20 (A$)"]) and row["ADV20 (A$)"] >= cfg.min_adv)
        rows.append(row)

    results = pd.DataFrame(rows)
    skipped_frame = pd.DataFrame(skipped, columns=["Ticker", "Reason"])
    return results, skipped_frame


def sector_strength(results: pd.DataFrame) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame(columns=["Sector", "Count"])
    grouped = results.groupby("Sector")
    table = pd.DataFrame({
        "Count": grouped.size(),
        "Median Ret 21D %": grouped["Ret 21D %"].median(),
        "Median Ret 63D %": grouped["Ret 63D %"].median(),
        "Median RSI14": grouped["RSI14"].median(),
        "% Above 200DMA": grouped["Above 200DMA"].mean() * 100.0,
        "Breakouts": grouped["Breakout"].sum(),
        "Breakdowns": grouped["Breakdown"].sum(),
    }).reset_index()
    table["Net Breadth"] = table["Breakouts"] - table["Breakdowns"]
    return table.sort_values("Median Ret 63D %", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

_SORT_KEYS = ["Confirmed", "VolumeRatio", "Ret 21D %"]


def _signal_table(results: pd.DataFrame, column: str, cfg: ScanConfig) -> pd.DataFrame:
    if results.empty:
        return results
    table = results[results[column] & results["Liquid"]].copy()
    if table.empty:
        return table
    ascending = [False, False, column == "Breakdown"]
    return table.sort_values(_SORT_KEYS, ascending=ascending).reset_index(drop=True)


def write_workbook(path: Path, breakouts: pd.DataFrame, breakdowns: pd.DataFrame,
                   sectors: pd.DataFrame, results: pd.DataFrame,
                   excluded: pd.DataFrame) -> None:
    sheets = {
        "Breakouts": breakouts,
        "Breakdowns": breakdowns,
        "Sector RS": sectors,
        "Full universe": results,
        "Excluded": excluded,
    }
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            out = frame if not frame.empty else pd.DataFrame({"": ["No rows"]})
            out.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.freeze_panes = "A2"
            for idx, column in enumerate(out.columns, start=1):
                values = out[column].astype(str)
                width = max(len(str(column)), int(values.str.len().max() or 0)) + 2
                sheet.column_dimensions[
                    sheet.cell(row=1, column=idx).column_letter
                ].width = min(max(width, 9), 42)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Breakout scan over the ASX listed universe, ex the largest N names.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--universe", required=True,
                        help="CSV of listed companies (ticker, name, sector, market cap); "
                             "a local path or an http(s) URL")
    parser.add_argument("--exclude-top", type=int, default=100,
                        help="Drop the N largest names by market cap")
    parser.add_argument("--min-adv", type=float, default=250_000.0,
                        help="Minimum 20-day average daily turnover in A$ to be signalled")
    parser.add_argument("--donchian", type=int, default=60,
                        help="Donchian channel lookback in sessions")
    parser.add_argument("--volume-multiple", type=float, default=2.0,
                        help="Volume vs 50-day average needed to confirm a signal")
    parser.add_argument("--history-days", type=int, default=420,
                        help="Calendar days of price history to request")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Tickers per download request; lower this if rate limited")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="Seconds between download batches")
    parser.add_argument("--prices", type=Path,
                        help="Re-scan from a cached price CSV instead of downloading")
    parser.add_argument("--cache-prices", type=Path,
                        help="Write downloaded history to this CSV for offline re-runs")
    parser.add_argument("--out", type=Path,
                        help="Output workbook (default: asx_breakout_scan_YYYY-MM-DD.xlsx)")
    parser.add_argument("--limit", type=int,
                        help="Only scan the first N names; useful for a smoke test")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = ScanConfig(donchian=args.donchian, volume_multiple=args.volume_multiple,
                     min_adv=args.min_adv)

    universe, excluded = load_universe(args.universe, exclude_top=args.exclude_top)
    if args.limit:
        universe = universe.head(args.limit).copy()
    print(f"Universe: {len(universe)} names to scan, {len(excluded)} excluded",
          file=sys.stderr)
    if universe.empty:
        print("Nothing left to scan after filtering.", file=sys.stderr)
        return 1

    tickers = universe["Ticker"].tolist()
    if args.prices:
        prices = {t: f for t, f in load_prices_csv(args.prices).items() if t in set(tickers)}
        print(f"Loaded cached history for {len(prices)} tickers", file=sys.stderr)
    else:
        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=args.history_days)
        print(f"Downloading {len(tickers)} tickers from {start} to {end}", file=sys.stderr)
        prices = download_prices(tickers, start, end,
                                 batch_size=args.batch_size, pause=args.pause)
        if args.cache_prices:
            save_prices_csv(prices, args.cache_prices)
            print(f"Cached price history to {args.cache_prices}", file=sys.stderr)

    results, skipped = scan(universe, prices, cfg)
    if results.empty:
        print("No names had enough history to scan.", file=sys.stderr)
        return 1

    breakouts = _signal_table(results, "Breakout", cfg)
    breakdowns = _signal_table(results, "Breakdown", cfg)
    sectors = sector_strength(results)

    illiquid = results.loc[~results["Liquid"], ["Ticker", "Name", "Sector", "MarketCap"]].copy()
    illiquid["Reason"] = f"Below A${cfg.min_adv:,.0f} average daily turnover"
    excluded_out = pd.concat([excluded, illiquid, skipped], ignore_index=True)

    out_path = args.out or Path(f"asx_breakout_scan_{date.today():%Y-%m-%d}.xlsx")
    write_workbook(out_path, breakouts, breakdowns, sectors, results, excluded_out)

    confirmed_up = int(breakouts["Confirmed"].sum()) if not breakouts.empty else 0
    confirmed_down = int(breakdowns["Confirmed"].sum()) if not breakdowns.empty else 0
    print(f"Scanned {len(results)} names "
          f"({int(results['Liquid'].sum())} above the turnover floor)", file=sys.stderr)
    print(f"Breakouts: {len(breakouts)} ({confirmed_up} volume-confirmed)", file=sys.stderr)
    print(f"Breakdowns: {len(breakdowns)} ({confirmed_down} volume-confirmed)", file=sys.stderr)
    print(f"Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
