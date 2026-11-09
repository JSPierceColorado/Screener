# V2 — S&P 100 Alpaca Screener (Railway)

**Short-lived hourly Railway Cron job** using `35 13-20 * * 1-5` (UTC). That covers **9:35, 10:35, 11:35, 12:35, 1:35, 2:35, 3:35 p.m./a.m. Eastern as appropriate**, plus one off-hours no-op per day across the DST seasons. The program gates on 09:30–16:00 New York local time and Alpaca's live calendar/market-open clock, so holidays, early closes and EST/EDT are handled automatically. Railway starts one process per cron invocation; it exits after the refresh or skip, so it does not run 24/7.

## Output: existing V2 `Screener` tab

| Column | Name | Definition |
| --- | --- | --- |
| A | `symbol` | Live S&P 100 symbols from the maintained Wikipedia Components table |
| B | `close` | Most recent **completed** US session's adjusted closing price |
| C | `sma_200` | Arithmetic mean of the last 200 split-adjusted daily closes |
| D | `sma_50` | Arithmetic mean of the last 50 split-adjusted daily closes |
| E | `pos_52w` | `(close - 252-session low) / (252-session high - low)`; 0 to 1 |
| F | `dollar_vol_m` | 20-session average of `daily_close × daily_share_volume / 1,000,000` |
| G | `score` | **NEVER CLEARED OR WRITTEN** (existing formulas remain in place) |

Each successful hourly run computes the same original V2 formula using **the last completed daily market session**, not live intraday prices. **Consequently values generally remain unchanged during the same trading day.** This is a scheduled refresh, not an intraday momentum model. To evolve intraday signals we should separately specify a compatible price/volume methodology.

Each run retrieves the current Symbols table from https://en.wikipedia.org/wiki/S%26P_100, so index changes follow that *community-maintained* page's updates (not an official index reconstitution feed). S&P 100 has historically contained 101 symbols owing to dual share classes. Validate table structure, 95–110 unique symbols, and Alpaca data availability before modifying the Sheet. Source unavailable, schema changed, Alpaca access denied or too few valid ticker bars? **Fail closed and keep existing data.** Never silently fall back to a stale hardcoded ticker list.

Data comes from Alpaca's `/v2/stocks/bars` with 1Day, `adjustment=split`, `feed=sip` by default, all pagination followed. You must have access to historical SIP data or explicitly change `ALPACA_DATA_FEED=iex`. IEX covers only IEX trades and causes a different, non-consolidated dollar volume reading; it is **not equivalent**.

### Safety / atomic replacement

After downloading and validating the complete list and market data, the application reads the Sheet's header and grid metadata. It performs **ONE Google Sheets `spreadsheets.batchUpdate`** containing two ordered `updateCells` operations:

1. Clear only `Screener!A2:F<last grid row>` (the old ~13,000-stock universe).
2. Fill only `Screener!A2:F<new final row>` with the fresh S&P 100 rows.

Both changes are in the same batch request. `G` is outside every range, so the `score` formulas in **G2:G13042** are not touched. Because G beyond the S&P100 list is preserved, unused lower rows' scores may remain zero or show their formula-defined blank result; this is intentional. The hidden `SellerState` tab is not accessed or edited.

## Setup

1. Create a Google Cloud project, enable the **Google Sheets API**, create a service account, and download its **JSON key**. Share the existing V2 Google Sheet (link below) with the JSON key's `client_email`, permission **Editor**. **Do not** paste any secrets in GitHub.
2. Use the existing private `JSPierceColorado/Screener` GitHub repository and connect it to a distinct `V2-Screener` Railway service; do not repurpose your separate credential-vault service. Railway should detect and build the Dockerfile.
3. In Railway service **Variables**, set `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, and `GOOGLE_SERVICE_ACCOUNT_JSON` (raw JSON on one line, including the `private_key` escaped `\n` sequences). Set `ALPACA_TRADING_BASE_URL` to the correct environment for your Alpaca key; default is paper API. Set `GOOGLE_SHEET_ID` if you ever use another workbook.
4. For a SIP-entitled account keep `ALPACA_DATA_FEED=sip`. Otherwise you may use `iex`, understanding that its volume metric is incomplete relative to the consolidated market.
5. **Deploy as a Railway Cron job** with `Cron Schedule = 35 13-20 * * 1-5`, `Start Command = python -u main.py --once`, `Restart policy = NEVER`, and one replica. Railway evaluates cron in UTC, but the code gates the actual NYSE session in America/New_York. Watch Railway deployment logs for `V2_HOURLY_CYCLE_DONE` and `SUCCESS` or `SKIPPED_NYSE` messages.
6. Optional manual execution from a Railway shell/local project: `python main.py --once` (market-calendar and market-open checks) or `python main.py --once --force` (manual refresh even on a closed day; uses previous session data).

**Target Sheet:** https://docs.google.com/spreadsheets/d/14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ/edit

### Environment variable reference

- `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` — required Alpaca credentials for historical data and calendar.
- `GOOGLE_SERVICE_ACCOUNT_JSON` — required Google service account credentials.
- `GOOGLE_SHEET_ID` — defaults to your V2 workbook.
- `ALPACA_TRADING_BASE_URL` — defaults to `https://paper-api.alpaca.markets`; live keys may require `https://api.alpaca.markets`.
- `ALPACA_DATA_FEED` — defaults to `sip`; `iex` can be used for IEX-only data.
- `MIN_VALID_SYMBOLS` — default `95`. If fewer tickers have the 252 needed bars, no data is cleared. Symbols with insufficient history are left blank in B:F and logged.

### Testing locally

```bash
pip install -r requirements.txt pytest
pytest -q
```

### Hourly schedule details

Railway UTC Cron: `35 13-20 * * 1-5`. Summer: 13:35–19:35 UTC is 09:35–15:35 ET, 20:35 UTC skips. Winter: 13:35 UTC skips, 14:35–20:35 UTC is 09:35–15:35 ET. Seven possible NYSE-hour refreshes per full trading day, zero on a closed exchange. The exact minute can vary slightly because Railway Cron starts may be delayed.

### Notes

- Uses the most recently completed daily bar. The whole update occurs after fetching data (a fetch failure cannot clear A–F).
- Strict output headers, per-row numeric validation, pagination handling, rate-limit retries.
- Logs show failures; configure Railway's alerting if you need push notifications on failures.
- No trading orders are submitted. This is a **read-only Alpaca market-data** job and a scoped write to the Google Sheet.
