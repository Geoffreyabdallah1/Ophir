# ASX ex-100 breakout scanner

A daily charting scan over the ASX listed universe with the largest names stripped
out — the small/mid-cap space where breakouts actually matter. It flags Donchian
channel breakouts and breakdowns, splits them into volume-confirmed and
unconfirmed, and reports moving-average structure, RSI, relative strength and
52-week extremes alongside. Output is a dated Excel workbook.

## Install and run

```bash
pip install -r requirements.txt
python asx_breakout_scan.py --universe asx_universe.csv
```

That writes `asx_breakout_scan_YYYY-MM-DD.xlsx` in the working directory.

## The universe file

`--universe` takes any CSV with a ticker column plus, ideally, company name,
sector and market cap. It accepts a local path or an `http(s)` URL.
[asxlistedcompanies.com](https://www.asxlistedcompanies.com/) publishes a CSV of
the full listed universe in exactly that shape.

Column names are auto-detected — `ASX code` / `Code` / `Ticker` / `Symbol` all
work, as do `Company name` / `Name`, `GICS industry group` / `Sector`, and
`Market Cap` (which parses `$1,234,567`, `1.5B` and `250m`). Then:

- Anything that isn't a three-character ordinary line is dropped, so options,
  rights, warrants and notes never reach the scan.
- The largest `--exclude-top` names by market cap are dropped. The default is
  100, so "ex-100" is computed from the data rather than depending on a stale
  index list. `--exclude-top 200` gives you ex-200.
- Names with no market cap are treated as small and stay in — a blank field
  never knocks a genuine small-cap out.

## Signals

| Signal | Definition |
| --- | --- |
| Breakout / Breakdown | Close above (below) the highest high (lowest low) of the prior 60 sessions. `--donchian` changes the lookback. |
| Confirmed | Today's volume at or above 2x the prior 50-day average. `--volume-multiple` changes the threshold. |
| Moving averages | Close vs the 20/50/200 DMA, as a percentage and as a flag. |
| Golden / death cross | 50DMA crossing the 200DMA within the last five sessions. |
| RSI14 | Wilder's RSI. |
| Returns | 5, 21 and 63 session price change. |
| 52-week extremes | Distance from the trailing 252-session high and low, plus a new-high/new-low flag tested against today's own intraday high and low. |
| ADV20 | 20-day average daily turnover in A$. |

The channel is built from intraday highs and lows rather than closes, so a close
that clears the prior range is a genuine break of the level, not just a
close-to-close move.

## Liquidity floor

`--min-adv` defaults to A$250,000 of 20-day average turnover. This matters more
than it sounds in ex-100 land — without it the list fills with untradeable shells
gapping on 50k of turnover. Raise it to 500000 or 1000000 if the output is still
noisy. Names below the floor are still scanned and still appear on the full
universe tab; they just don't reach the signal tabs, and they're listed on the
excluded tab with the reason.

## Output

Five tabs:

- **Breakouts** — volume-confirmed first, then by volume ratio and 21-day return.
- **Breakdowns** — same ordering, weakest first.
- **Sector RS** — count, median 21/63-day return, median RSI, share above the
  200DMA, and breakout minus breakdown breadth per sector, ranked on 63-day return.
- **Full universe** — every scanned name with all metrics.
- **Excluded** — what was dropped and why: not an ordinary line, top N by market
  cap, below the turnover floor, or too little price history.

## Offline re-runs

Price history comes from Yahoo Finance via `yfinance`. Downloading ~1,700 names
takes a few minutes, so cache it once and re-scan as often as you like:

```bash
python asx_breakout_scan.py --universe asx_universe.csv --cache-prices prices.csv
python asx_breakout_scan.py --universe asx_universe.csv --prices prices.csv --min-adv 1000000
```

If the first live run hits rate limiting, lower `--batch-size` (default 50) and
raise `--pause` (default 1 second between batches). `--limit N` scans only the
first N names, which is the quickest way to check the download path works before
committing to the full universe.

## Tests

```bash
python -m unittest discover -s tests
```

The signal engine is tested against synthetic series — breakouts, breakdowns,
volume confirmation, the channel excluding today's own bar, RSI bounds, turnover,
the liquidity gate, universe parsing and the top-N exclusion. The `yfinance`
download path is not covered, since it needs live network access.
