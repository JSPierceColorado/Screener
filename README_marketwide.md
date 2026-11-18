# Marketwide Momentum — isolated Alpaca-wide scanner + buyer

**Two new Railway services** backed by the **marketwide-momentum-v1** branch of the existing Screener repository. Existing V2 screener and V2 buyer source, timing, low-water ladder and the single live profit-only seller are not modified.

## Inputs & exact V2 scoring

- Enumerate Alpaca \`GET /v2/assets?status=active&asset_class=us_equity\`, considering every active/tradable U.S. equity stock, including non-fractionable stock symbols in research (the buyer separately requires fractional eligibility). Exclude ETF/ETN instruments by asset name (Alpaca's us_equity class contains funds), and reserve VT/SGOV exclusively for the cash-funded Compounder. Stocks that lack 252 completed trading daily bars get score **0**, not an invented score.
- Scan 480 calendar days of split-adjusted Alpaca multi-symbol 1Day bars, 20 stocks per request with all next-page tokens processed; never include the current day's incomplete daily bar. Alpaca's multi-symbol API limit counts **total bars**, not bars per symbol. Pacing .55 seconds/request keeps well below 200 requests/minute. The first broad scan can take several minutes. The source feed should match V2 Screener's current authorized feed; no silent SIP-to-IEX quality fallback.
- Original V2 Screener!G formula, ported exactly:
  - eligibility: close>0, SMA50>0, SMA200>0, daily dollar volume >= $10M, close > SMA200, SMA50 > SMA200, 0 <= close/SMA50−1 <= .25, close/SMA200−1 <= .60, 52-week position >= .75.
  - score = \`10 + 35*max(0,min(1,g50/.05,(.25-g50)/.1)) + 20*max(0,min(1,g200/.1,(.6-g200)/.15)) + 25*max(0,min(1,(rangePosition-.75)/.2,(1-rangePosition)/.015)) + 10*min(1,ln(1+dollarVolumeM)/ln(101))\`.
  - four-decimal rounding and original threshold **>=95**; 50/200 SMA, 52-week high-low and 20-day dollar volume inputs are rounded to 4 decimals before scoring as in V2.
- Independent \`Marketwide\` tab of the **existing V2 workbook** (spreadsheet ID 14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ), never overwrite \`Screener\` or \`SellerState\`. Headers: \`symbol close sma_200 sma_50 pos_52w dollar_vol_m score completed_session_date scan_utc\`.
- Broad universe refresh at **7:00 AM America/Denver** Monday–Friday. Full input is checked BEFORE atomic owned-tab publish. A failed scan preserves the last-good snapshot; buyer demands scan_utc from TODAY and completed_session_date from Alpaca's previous scheduled session.

## New marketwide buyer

- Independent Railway process and **dedicated /data persistent SQLite** order ledger.
- Buys only new **Alpaca-tradable, fractionable, marginable** stock positions with score **>=95**. Excludes any stock on the original V2 S&P100 \`Screener\` list, all already-held account positions, open-order symbols, broker-ineligible securities, VT/SGOV, and historical filled-stop cooldowns through their old expiry. S&P100 remains exclusively owned by V2 to preserve its 2% sizing and low-water strategy.
- **0.1% of remaining account margin buying power** for each new position, rounded DOWN to cents; minimum broker notional $1; hard cap **10 new submitted positions per trading day** and **150 total account positions** across strategies (important because the seller has no loss stops). From $10k available margin power, a typical initial ticket is $10, and a full 10-order batch would consume roughly $100 of buying power. Margin can finance these positions.
- Uses deterministic IDs \`mwbu-YYYYMMDD-SYMBOL\`, broker order + portfolio read before POST, persistent RESERVED row **before** sending POST, one POST only. Ambiguous responses freeze the next attempt until the broker confirms a status; no silent blind retry. A canceled/rejected attempt still counts toward the day's cap.
- Checks at **11:00, 11:15, 11:30 and 11:45 AM Denver**. Market-open check guards orders; holiday or market closure cannot trigger orders; a successful first scan does not need reorders.
- New buyer has **NO selling code**. Existing V2-Seller manages all new positions under its current **profit-only +13%-arm / 3-percentage-point trailing giveback / >=10% actual gain DAY limit** policy. Automated loss stop orders remain disabled for every symbol. VT and SGOV are never sold by that seller.
- This strategy does **not** extend the S&P100 six-step low-water rebuy ladder to non-S&P100 securities. Small new positions remain small unless another expressly authorized strategy buys them separately.

## Railway setup

Deploy **two separate services** in the Investing project from \`JSPierceColorado/Screener\` branch \`marketwide-momentum-v1\`. Docker runs \`python -u marketwide_service.py\`; the role is required and never defaults to placing orders.

For Screener set \`MARKETWIDE_ROLE=screener\`, reference V2-Screener's Alpaca read-only data credentials, \`ALPACA_TRADING_BASE_URL=https://api.alpaca.markets\`, \`GOOGLE_SERVICE_ACCOUNT_JSON\` and \`GOOGLE_SHEET_ID\` from V2-Screener, and \`ALPACA_DATA_FEED\` via a reference to V2-Screener's feed. \`MARKETWIDE_SCAN_ON_BOOT\` is optional, default false. One always-on instance, no Railway cron.

For Buyer set \`MARKETWIDE_ROLE=buyer\`, reference V2-Buyer's live trading keys, same Google Sheets account as V2-Screener, \`ALPACA_TRADING_BASE_URL=https://api.alpaca.markets\`, \`ALPACA_PAPER=false\`, \`DRY_RUN=true\` initially, and attach persistent volume \`/data\`. Enable live orders only after a **verified full marketwide score snapshot and passing dry-run** with \`ENABLE_LIVE_TRADING=true\`, \`LIVE_TRADING_ACK=I_ACCEPT_REAL_MONEY_BUYS\`, \`DRY_RUN=false\`.

DO NOT merge this branch to main without separately accounting for the V2-Screener service's Dockerfile source; branch isolation is intentional.

## Operations

- Screener: \`MARKETWIDE_SCREENER_STARTED\`, \`MARKETWIDE_FETCH_PROGRESS\`, \`MARKETWIDE_SCREEN_COMPLETE\`, \`MARKETWIDE_SCREEN_FAILED\`.
- Buyer: \`MARKETWIDE_BUYER_LEDGER_READY\`, \`MARKETWIDE_BUYER_STARTED\`, \`MARKETWIDE_BUY_SIGNALS\`, \`MARKETWIDE_BUY_SUBMITTED\`, \`MARKETWIDE_BUY_AUDIT\`, \`MARKETWIDE_POST_UNCERTAIN\`, \`MARKETWIDE_RECONCILIATION_REQUIRED\`, \`MARKETWIDE_BUYER_FAILED\`.
- Logs show submitted orders, not confirmed fills. Brokerage positions/orders and the persistent ledger are authoritative for fill confirmation.
- Future position-level performance attribution should treat \`mwbu-\` orders as a distinct strategy, not mix them silently with V2 buys, ATLAS exploratories, or the VT/SGOV Compounder.

Run \`python -m pytest -q\` and \`python -m compileall -q main.py marketwide_common.py marketwide_screener.py marketwide_buyer.py marketwide_service.py\`.

**Risk:** More holdings do not guarantee higher returns. The 95+ score is historical momentum and can reverse, and the account uses margin. No-stop-loss profit-only exits may indefinitely retain declining positions; monitor equity, net return after margin interest, exposure, holdings, and maximum drawdown.
