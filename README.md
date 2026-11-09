# V2 — S&P 100 Screener (always-on 9 AM Mountain worker)

Runs as one **persistent Railway worker** with an internal APScheduler job every weekday at **9:00 AM America/Denver**; the timezone automatically accounts for DST. The program checks Alpaca's NYSE calendar and trading clock and excludes incomplete current-day bars, so holidays and closures are skipped. It uses the original V2 completed-daily-candle scoring methodology, not hourly intraday pricing.

## Manual testing

Use `python -u main.py --once --force` to test a single refresh **now**, outside the scheduled 9 AM window if needed. This preserves the safeguard that the current day's daily bar must be completed. `python -u main.py --once` runs once but still checks the 9 AM and exchange-open gates.

A manual invocation prints `V2_MANUAL_RUN_COMPLETED refreshed=True` on success. Running `python -u main.py` starts the permanent scheduler; it does **not** run a scan immediately on each restart.

## Railway

Project: Investing. Service: V2-Screener. Source: `JSPierceColorado/Screener`.
Use **no Railway Cron schedule**, start command `python -u main.py`, restart policy `ALWAYS`, one replica, no public domain. The worker stays up all day rather than exiting between scans.

Alpaca is used ONLY for read-only calendar, clock, and historical market data. The credentials may be from the live trading account without giving this screener any order-submission code.
Required environment variables: `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `ALPACA_TRADING_BASE_URL=https://api.alpaca.markets`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_SHEET_ID=14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ`, `ALPACA_DATA_FEED=sip` (only if your entitlement supports it), `MIN_VALID_SYMBOLS=95`, `LOG_LEVEL=INFO`.

## Spreadsheet

[Original V2 Screener](https://docs.google.com/spreadsheets/d/14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ/edit)

S&P100 symbols (typically 101 rows) come from a validated public component list. Only `Screener!A2:F` is cleared and written in one atomic Sheets request. `Screener!G` scoring formulas and the `SellerState` tab are untouched. `close`, SMA50, SMA200, 52-week range and average dollar-volume all use the last completed trading session. A Monday morning scan generally reflects Friday's session.

## Verify

```bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
python -m compileall -q main.py
```

This service has no buy/sell/order endpoints. The separate seller bot makes the risk-managed live/paper trading decisions.
