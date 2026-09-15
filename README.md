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

That writes `asx_breakout_scan_YYYY-MM-DD.xlsx` in the working directory. The
universe can be as little as a text file of ASX codes, one per line:

```bash
printf 'SXE\nSLC\nIPG\nVEA\nLOV\n' > watchlist.csv
python asx_breakout_scan.py --universe watchlist.csv --exclude-top 0
```

Price history is downloaded from Yahoo Finance, so the machine running this needs
outbound access to `query1.finance.yahoo.com`, `query2.finance.yahoo.com` and
`fc.yahoo.com`. On a restricted network the download fails with
`CONNECT tunnel failed, response 403` and no prices come back.

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
- The largest `--exclude-top` names are dropped. The default is 100, so "ex-100"
  is computed from the data rather than depending on a stale index list.
  `--exclude-top 200` gives you ex-200, and `--exclude-top 0` scans everything.
- Names with no market cap are treated as small and stay in — a blank field
  never knocks a genuine small-cap out.

If the file has no market cap column at all — a bare list of codes, say — the
top-N cut falls back to 60-day average turnover, which is a serviceable proxy
for size and needs no extra data. `--rank-by` forces the choice:

| Value | Behaviour |
| --- | --- |
| `auto` (default) | Market cap when the file carries enough of it, else turnover. |
| `market-cap` | Always market cap. Nothing is dropped if the column is missing. |
| `turnover` | Always 60-day average daily turnover, computed from the price data. |

Turnover ranking happens after the download, since it needs the prices; the run
log says which basis was used.

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
| ADV20 / ADV60 | 20- and 60-day average daily turnover in A$. The 60-day figure rides out a single block trade or capital raising, which is why the size ranking uses it. |

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

## FactSet

`factset_client.py` is a standalone client for the FactSet API — independent of
the scanner, usable on its own. It handles OAuth 2.0 authentication and wraps
Global Prices and Fundamentals, with a generic escape hatch for anything else
under `api.factset.com`.

```bash
pip install -r requirements-factset.txt
```

That extra install is deliberate: the scanner itself doesn't need FactSet, so
`requirements.txt` stays lean.

### Credentials

Register an application at [developer.factset.com](https://developer.factset.com)
and download its JSON config. FactSet issues confidential clients in two
flavours; the **Type** column on the API Authentication page says which you
have, and both work here:

| Portal type | Config carries | Extra install |
| --- | --- | --- |
| `... Machine Authorization (Key Pair)` | a `jwk` block holding an RSA private key | yes — signing helper |
| `... Machine Authorization (Client Secret)` | a shared secret string | no |

A Key Pair application signs a JWT to request its token, which needs FactSet's
signing helper. A Client Secret application posts its secret to the token
endpoint, which needs nothing beyond `requests` — `requirements-factset.txt` is
optional in that case.

Either way the config is a secret. The client looks for it in this order:

1. the `config_path` argument,
2. `$FACTSET_CONFIG_PATH`,
3. `./factset.json`,
4. `~/.factset/config.json`.

```bash
mkdir -p ~/.factset && mv ~/Downloads/factset-*.json ~/.factset/config.json
chmod 600 ~/.factset/config.json
export FACTSET_CONFIG_PATH=~/.factset/config.json
```

For a Client Secret application the portal shows the secret once, separately
from the JSON. Either add it to the config as `"clientSecret"`, or — better —
keep it out of the file entirely:

```bash
export FACTSET_CLIENT_SECRET='...'
```

`$FACTSET_CLIENT_SECRET` wins over the file when both are set.

`factset.json` and `factset-*.json` are gitignored. Nothing in the module logs,
prints or echoes credentials, and `--check` reports that a token was obtained
without ever printing it.

### Setup

```bash
./setup_factset.sh
```

Prompts for the Client ID and Secret from developer.factset.com, writes them to
`~/.factset/config.json` with owner-only permissions, and runs the connection
check. That is one of the paths the client searches by default, so there are no
environment variables to export and no shell profile to edit — it keeps working
in every new terminal. The secret is read without echoing and never reaches your
shell history.

### Checking the connection

```bash
python factset_client.py --check
```

Walks the three things that can be wrong, in order, and exits non-zero at the
first failure:

```
1. Credentials    config found, client id, which flavour, where the secret came from
2. Authentication a token was obtained, and when it expires
3. Data access    a real price call, confirming entitlements
```

`--config` points at a config elsewhere; `--symbol` changes the symbol used for
the data check, which matters if your entitlements don't cover Australia.

### Use

```python
from factset_client import FactSetClient, asx_symbols

with FactSetClient() as fs:
    prices = fs.prices(asx_symbols(["SXE", "SLC", "IPG"]),
                       start_date="2024-01-01", end_date="2024-12-31")
    actions = fs.corporate_actions("BHP-AU", event_category="SPLITS")
    sales = fs.fundamentals("BHP-AU", metrics=["FF_SALES"], periodicity="ANN")
```

Every wrapper returns a DataFrame. `asx_symbol` maps an ASX code to FactSet's
ticker-region form — `SXE` becomes `SXE-AU` — and leaves an already-qualified
symbol alone.

| Method | Endpoint |
| --- | --- |
| `prices` | OHLCV history, with frequency, currency and split adjustment. |
| `returns` | Period-by-period total returns. |
| `corporate_actions` | Dividends, splits, spinoffs, rights. |
| `shares_outstanding` | Historical share counts. |
| `fundamentals` | Financial statement data for `FF_*` metric codes. |
| `fundamentals_metrics` | The metric catalogue, for looking codes up first. |
| `get` / `post` | Any other FactSet path. |

Id lists are split into batches of 50 — the Global Prices ceiling for multi-day
requests — and stacked back into one frame, so a full universe is a single call.
`429` and `5xx` are retried with backoff, honouring `Retry-After`. A `401` or
`403` raises `FactSetAuthError` immediately rather than retrying, since a
credential or entitlement problem won't resolve itself.

### Network access

FactSet is reached over `api.factset.com` and `auth.factset.com`. On a
restricted network both fail at the proxy with
`CONNECT tunnel failed, response 403`, the same way the Yahoo download does, and
no data comes back. Those two hosts need to be allowed outbound wherever this
runs.

## Tests

```bash
python -m unittest discover -s tests
```

The signal engine is tested against synthetic series — breakouts, breakdowns,
volume confirmation, the channel excluding today's own bar, RSI bounds, turnover,
the liquidity gate, universe parsing, the top-N exclusion on either basis, and
workbook writing with empty columns. The frame reshaping
that sits between `yfinance` and the signal pass is covered too: both MultiIndex
column orderings, a single flat-column ticker, a partly failed batch, and an
`Adj Close` column arriving alongside `Close`. The network call itself is not
covered, since it needs live access to Yahoo.

The FactSet client is covered too, against a fake transport: credentials validation
for both flavours and its error messages, bearer auth, id batching at 50, retry and
backoff on `429` and `5xx`, `401`/`403` raising immediately, error-body parsing, and
the request body of each endpoint wrapper. The client-secret flow is covered end to
end — metadata discovery, the form-post token exchange, the Basic-auth fallback,
token caching and renewal before expiry, and a rejected secret. What is not covered
is a real exchange with FactSet's own authorization server, which needs live
credentials and outbound access.
