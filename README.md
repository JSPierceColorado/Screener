# V2 — Original S&P 100 Daily Screener

The original **V2 Screener** runs **once per U.S. stock-market trading day, at approximately 9:00 a.m. America/Denver** (11:00 a.m. New York). It uses only **the last COMPLETED daily market bar**, not the current intraday price, and preserves all existing Google Sheets score formulas.

## Schedule

Railway Cron runs on **UTC**, not Mountain time, so the service schedules two potential start times:

```cron
0 15,16 * * 1-5
```

- Summer (MDT, UTC−6): **15:00 UTC = 9:00 a.m. Mountain**. The 16:00 UTC invocation is skipped.
- Winter (MST, UTC−7): the 15:00 UTC invocation is skipped; **16:00 UTC = 9:00 a.m. Mountain**.
- The application permits a scheduled refresh **only during 9:00–9:59 a.m. Denver time**, with allowance for small cron delays.
- Alpaca's trading calendar and market-open clock must confirm a real open NYSE session. Weekends, market holidays, exchange closures, and out-of-session runs do **not** modify the Sheet.
- Railway is configured as a **short-lived cron worker**, with start command `python -u main.py --once` and `restartPolicyType=NEVER`. The process exits after refreshing or skipping; no always-on scheduler is required.

This reproduces the original V2 **daily research cadence** without wasting resources on hourly same-data refreshes. Railway's Cron scheduling isn't guaranteed to the minute.

## Source and spreadsheet

**Spreadsheet:** https://docs.google.com/spreadsheets/d/14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ/edit

The worker pulls the latest S&P 100 symbols from the Wikipedia Components table (community-maintained, not official exchange membership), validates roughly 95–110 symbols, downloads Alpaca `1Day` historical bars, and computes:

| Column | Field | Definition |
| --- | --- | --- |
| A | `symbol` | Latest sourced S&P 100 symbols |
| B | `close` | **Last completed market session's close** |
| C | `sma_200` | Mean of the previous 200 completed daily closes |
| D | `sma_50` | Mean of the previous 50 completed daily closes |
| E | `pos_52w` | Position of last close within previous 252-session low/high |
| F | `dollar_vol_m` | Mean daily dollar volume over 20 completed sessions, in millions |
| G | `score` | Existing Google Sheets formulas; **never edited by the worker** |

Today's partly formed daily bar is excluded even if Alpaca includes it in the API response. Therefore a Monday morning scan normally uses **Friday's completed session**.

All inputs are downloaded and validated **before** any modification. The worker performs a single atomic Sheets `batchUpdate` that clears/writes only `Screener!A2:F` while leaving all `G` formulas and the `SellerState` tab untouched. If market data, list validation, credentials, or Sheets headers fail, it aborts and retains existing data.

## Railway service

- Project: `Investing`
- Service: `V2-Screener`
- GitHub source: `JSPierceColorado/Screener`, branch `main`
- Cron: `0 15,16 * * 1-5` (UTC)
- Start: `python -u main.py --once`
- Restart: `NEVER`
- Runtime: short-lived; zero replicas between completed cron runs

Reference credentials through your credential-holder service (do not paste values into GitHub):

| Environment variable | Purpose |
| --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google service-account JSON with edit access to V2 |
| `APCA_API_KEY_ID` | Alpaca market-data authorization |
| `APCA_API_SECRET_KEY` | Alpaca market-data secret |
| `GOOGLE_SHEET_ID` | `14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ` |
| `ALPACA_TRADING_BASE_URL` | `https://paper-api.alpaca.markets` for paper credentials |
| `ALPACA_DATA_FEED` | `sip` if historical SIP access is entitled; `iex` only with awareness of different coverage |
| `MIN_VALID_SYMBOLS` | `95` |
| `LOG_LEVEL` | `INFO` |

A successful invocation logs `SUCCESS: ... G untouched` and `V2_DAILY_CYCLE_DONE refreshed=True`. A scheduled off-hour invocation logs `SKIPPED_OUTSIDE_ORIGINAL_DAILY_9AM_MOUNTAIN` and exits without API calls.

**No brokerage orders are submitted.** V2 is a read-only screener and a scoped Google Sheets writer.

## Testing and manual refresh

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
python -m compileall -q main.py
```

A deliberate manual run can use `python main.py --once --force` to bypass the calendar/time gate while still using only the previous completed daily bar; do not make `--force` part of the production schedule.
