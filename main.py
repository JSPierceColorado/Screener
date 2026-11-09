"""V2 daily S&P100 screener — continuously running 9AM Mountain scheduler.

One long-running Railway worker schedules one scan each trading weekday at
09:00 America/Denver via APScheduler, automatically following daylight saving.
Manual --once --force runs a one-off integrity check outside that schedule.
Only the last COMPLETED daily bar is used. Screener!G remains untouched.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, time, timezone
from statistics import mean
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("v2_sp100")
DENVER = ZoneInfo("America/Denver")
NEW_YORK = ZoneInfo("America/New_York")
SPREADSHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ")
SHEET_NAME = "Screener"
WIKIPEDIA_PAGE = "https://en.wikipedia.org/wiki/S%26P_100"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
EXPECTED_HEADERS = ["symbol", "close", "sma_200", "sma_50", "pos_52w", "dollar_vol_m", "score"]


def http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",), respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "V2-SP100-Screener/1.0 (market-research automation; https://github.com/JSPierceColorado)",
                            "Accept": "text/html,application/json"})
    return session


def response_json(session: requests.Session, url: str, **kwargs: Any) -> Any:
    response = session.get(url, timeout=(15, 90), **kwargs)
    response.raise_for_status()
    return response.json()


def parse_constituents(html: str) -> list[str]:
    """Find Wikipedia's actual Components table by column headers, not position."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        heads = [c.get_text(" ", strip=True).lower() for c in header_row.find_all(["th", "td"], recursive=False)]
        if not (len(heads) >= 3 and heads[0] in ("symbol", "ticker", "ticker symbol")
                and heads[1] in ("name", "company", "security") and "sector" in heads[2]):
            continue
        symbols: list[str] = []
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) < 3:
                continue
            ticker = re.sub(r"\[\d+\]", "", cells[0].get_text(" ", strip=True)).strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
                raise ValueError(f"Unrecognized symbol in the published S&P 100 table: {ticker!r}")
            symbols.append(ticker)
        if not (95 <= len(symbols) <= 110 and len(symbols) == len(set(symbols))):
            raise ValueError(f"S&P 100 source unexpectedly returned {len(symbols)} symbols / duplicates; refusing to clear Sheet")
        # S&P 100 currently contains 101 tickers due to dual share classes.
        return sorted(symbols)
    raise ValueError("Could not identify the Wikipedia S&P 100 Components table; refusing to clear Sheet")


def get_sp100_symbols(session: requests.Session) -> list[str]:
    errors: list[str] = []
    try:
        resp = session.get(WIKIPEDIA_PAGE, timeout=(15, 45))
        resp.raise_for_status()
        return parse_constituents(resp.text)
    except (requests.RequestException, ValueError) as exc:
        errors.append(f"HTML source: {exc}")

    # Alternative route to the *same live Wikipedia page*, not a hardcoded backup list.
    try:
        result = response_json(session, WIKIPEDIA_API, params={
            "action": "parse", "page": "S&P_100", "prop": "text",
            "format": "json", "formatversion": 2,
        })
        return parse_constituents(result["parse"]["text"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        errors.append(f"MediaWiki API: {exc}")
    raise RuntimeError("Failed to fetch a validated current S&P 100 list; no Sheet changes. " + "; ".join(errors))


def alpaca_headers() -> dict[str, str]:
    key = os.environ.get("APCA_API_KEY_ID", "").strip()
    secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
    if not (key and secret):
        raise RuntimeError("Railway variables APCA_API_KEY_ID and APCA_API_SECRET_KEY are required")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def alpaca_trading_base() -> str:
    base = os.environ.get("ALPACA_TRADING_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("ALPACA_TRADING_BASE_URL must be HTTPS")
    return base


def within_original_daily_scan_window(now: datetime) -> bool:
    """Confirm this is a scheduled 9 AM Mountain scan, across MST/MDT.

    Called by the daily APScheduler job as a secondary safeguard. A manual
    --once --force command explicitly bypasses this date/time restriction.
    """
    local = now.astimezone(DENVER)
    return local.weekday() < 5 and local.hour == 9


def is_market_day(session: requests.Session, current_date) -> bool:
    """Fail closed on calendar errors, and skip weekends/exchange holidays."""
    if current_date.weekday() >= 5:
        return False
    payload = response_json(session, alpaca_trading_base() + "/v2/calendar", params={
        "start": current_date.isoformat(), "end": current_date.isoformat(),
    }, headers=alpaca_headers())
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected response from Alpaca exchange calendar; aborting")
    has_session = any(isinstance(day, dict) and day.get("date") == current_date.isoformat() for day in payload)
    if not has_session:
        return False
    # The market clock covers unexpected early closures or special halts.
    clock = response_json(session, alpaca_trading_base() + "/v2/clock", headers=alpaca_headers())
    if not isinstance(clock, dict) or not isinstance(clock.get("is_open"), bool):
        raise RuntimeError("Unexpected response from Alpaca market clock; aborting")
    return clock["is_open"]


def get_daily_bars(session: requests.Session, symbols: list[str], today_ny) -> dict[str, list[dict]]:
    # At 09:00 Denver / 11:00 New York, today's daily bar is INCOMPLETE.
    # Alpaca's 'end' is inclusive. Set it to the final second of yesterday in NY,
    # then additionally filter timestamps locally to dates strictly before today.
    midnight_today = datetime.combine(today_ny, time.min, tzinfo=NEW_YORK)
    end = (midnight_today - timedelta(seconds=1)).isoformat()
    start = (today_ny - timedelta(days=480)).isoformat()
    params: dict[str, Any] = {
        "symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
        "end": end, "limit": 10000, "adjustment": "split",
        "feed": os.environ.get("ALPACA_DATA_FEED", "sip"),
    }
    combined: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    seen: set[str] = set()
    for _ in range(30):
        result = response_json(session, DATA_URL, params=params, headers=alpaca_headers())
        if not isinstance(result, dict) or not isinstance(result.get("bars"), dict):
            raise RuntimeError("Alpaca data response missing bars mapping; no Sheet changes")
        for symbol, bars in result["bars"].items():
            if symbol in combined:
                combined[symbol].extend(bars)
        next_token = result.get("next_page_token")
        if not next_token:
            break
        if next_token in seen:
            raise RuntimeError("Alpaca repeated a pagination token; aborting")
        seen.add(next_token)
        params["page_token"] = next_token
    else:
        raise RuntimeError("Alpaca returned too many data pages; aborting")

    for symbol, bars in combined.items():
        filtered: dict[str, dict] = {}
        for bar in bars:
            try:
                timestamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
                session_date = timestamp.astimezone(NEW_YORK).date()
                if session_date >= today_ny:
                    continue
                if not all(math.isfinite(float(bar[field])) for field in ("c", "h", "l", "v")):
                    continue
                if float(bar["c"]) <= 0 or float(bar["h"]) <= 0 or float(bar["l"]) <= 0 or float(bar["v"]) < 0:
                    continue
                filtered[session_date.isoformat()] = bar
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        combined[symbol] = [filtered[day] for day in sorted(filtered)]
    return combined


def calculate_rows(symbols: list[str], bars_by_symbol: dict[str, list[dict]]) -> tuple[list[list], list[str]]:
    rows: list[list] = []
    missing: list[str] = []
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol, [])
        if len(bars) < 252:
            rows.append([symbol, "", "", "", "", ""])
            missing.append(f"{symbol} ({len(bars)} sessions)")
            continue
        last = bars[-1]
        last252 = bars[-252:]
        low52 = min(float(b["l"]) for b in last252)
        high52 = max(float(b["h"]) for b in last252)
        pos52 = (float(last["c"]) - low52) / (high52 - low52) if high52 > low52 else 0
        close = float(last["c"])
        sma200 = mean(float(b["c"]) for b in bars[-200:])
        sma50 = mean(float(b["c"]) for b in bars[-50:])
        dollar_vol_m = mean(float(b["c"]) * float(b["v"]) for b in bars[-20:]) / 1_000_000
        rows.append([symbol, round(close, 4), round(sma200, 4), round(sma50, 4),
                     round(max(0.0, min(1.0, pos52)), 4), round(dollar_vol_m, 4)])
    good = len(rows) - len(missing)
    min_good = int(os.environ.get("MIN_VALID_SYMBOLS", "95"))
    if good < min(min_good, len(symbols)):
        raise RuntimeError(f"Only {good}/{len(symbols)} symbols have 252 daily bars; "
                           "refusing to erase existing Screener data: " + ", ".join(missing))
    return rows, missing


def validated_completed_session(bars_by_symbol: dict[str,list[dict]], symbols:list[str],
                                today_ny) -> str:
    """Demand a recent, broadly shared completed session before updating scores.

    The buyer must never use an old sheet merely because today's scheduled
    Python function fired. Disallow bar sets where <95 symbols share a date.
    """
    latest=[]
    for sym in symbols:
        seq=bars_by_symbol.get(sym,[])
        if len(seq)<252:
            continue
        raw=seq[-1].get("t")
        if not isinstance(raw,str):
            raise RuntimeError("Missing daily market bar timestamp")
        dt=datetime.fromisoformat(raw.replace("Z","+00:00"))
        if dt.tzinfo is None:
            raise RuntimeError("Daily bar date is timezone naive")
        d=dt.astimezone(NEW_YORK).date()
        if d>=today_ny:
            raise RuntimeError("Partial current-day bar detected")
        latest.append(d)
    if not latest:
        raise RuntimeError("No completed-session bars")
    date,count=Counter(latest).most_common(1)[0]
    if count<95 or (today_ny-date).days>7:
        raise RuntimeError("Stale/incomplete completed daily market session")
    return date.isoformat()


def build_sheets_client():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is required; share the Sheet with its client_email")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON must be unencoded JSON") from exc
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def build_atomic_update(sheet_id: int, row_count: int, rows: list[list]) -> list[dict]:
    """Clear A2:F(last sheet row), then write new A:F, as one atomic API batch."""
    if row_count < len(rows) + 1:
        raise ValueError("Screener grid does not have sufficient rows; refusing to alter structure")
    if any(len(row) != 6 for row in rows):
        raise ValueError("Expected exactly six columns of screener values (A:F)")
    def cell(value: Any) -> dict:
        if isinstance(value, str):
            return {"userEnteredValue": {"stringValue": value}}
        if isinstance(value, (float, int)) and math.isfinite(float(value)):
            return {"userEnteredValue": {"numberValue": value}}
        raise ValueError(f"Unexpected cell type: {type(value).__name__}")
    return [
        {"updateCells": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": row_count,
                      "startColumnIndex": 0, "endColumnIndex": 6},
            "rows": [], "fields": "userEnteredValue",
        }},
        {"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": 1, "columnIndex": 0},
            "rows": [{"values": [cell(v) for v in row]} for row in rows],
            "fields": "userEnteredValue",
        }},
    ]


def update_screener(service, rows: list[list], *, scan_utc: str | None = None,
                    completed_session: str | None = None) -> None:
    sheets = service.spreadsheets()
    # requests.Session retries do not cover calls made by googleapiclient.
    # Retry transient Sheets 429/5xx responses with randomized exponential backoff.
    metadata = sheets.get(spreadsheetId=SPREADSHEET_ID,
                          fields="sheets(properties(sheetId,title,gridProperties(rowCount)))").execute(num_retries=5)
    target = next((s["properties"] for s in metadata["sheets"]
                   if s["properties"]["title"] == SHEET_NAME), None)
    if target is None:
        raise RuntimeError(f"Missing {SHEET_NAME!r} tab. Refusing to write")
    headers = sheets.values().get(spreadsheetId=SPREADSHEET_ID,
                                  range=f"'{SHEET_NAME}'!A1:G1").execute(num_retries=5).get("values", [[]])[0]
    if headers != EXPECTED_HEADERS:
        raise RuntimeError(f"Unexpected Screener headers: {headers!r}; refusing to write")
    requests_batch = build_atomic_update(target["sheetId"], target["gridProperties"]["rowCount"], rows)
    if (scan_utc is None)!=(completed_session is None):
        raise ValueError("Scan metadata must have timestamp and data-session date")
    if scan_utc:
        # Metadata is in I:J and in the SAME atomic Sheets write as A:F.
        # G (original score formula) and H remain untouched.
        datetime.fromisoformat(scan_utc.replace("Z","+00:00"))
        datetime.fromisoformat(completed_session)
        def stringcell(s):
            return {"userEnteredValue":{"stringValue":s}}
        requests_batch.append({"updateCells":{
            "start":{"sheetId":target["sheetId"],"rowIndex":0,"columnIndex":8},
            "rows":[{"values":[stringcell("last_scan_utc"),
                                stringcell("completed_session_date")]},
                    {"values":[stringcell(scan_utc),stringcell(completed_session)]}],
            "fields":"userEnteredValue"
        }})
    sheets.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": requests_batch}).execute(num_retries=5)


def refresh(session: requests.Session | None = None, *, force: bool = False, now: datetime | None = None) -> bool:
    session = session or http_session()
    now = now or datetime.now(DENVER)
    current_date = now.astimezone(NEW_YORK).date()
    if not force and not within_original_daily_scan_window(now):
        LOG.info("SKIPPED_OUTSIDE_ORIGINAL_DAILY_9AM_MOUNTAIN: denver_time=%s",
                 now.astimezone(DENVER).isoformat())
        return False
    if not force and not is_market_day(session, current_date):
        LOG.info("SKIPPED_NYSE_CALENDAR_OR_CLOCK: date=%s", current_date)
        return False
    # IMPORTANT: Obtain and validate EVERY input before any Sheet writes.
    symbols = get_sp100_symbols(session)
    LOG.info("Current online S&P 100 constituent list: %d symbols", len(symbols))
    bars_by_symbol = get_daily_bars(session, symbols, current_date)
    rows, missing = calculate_rows(symbols, bars_by_symbol)
    completed_session = validated_completed_session(bars_by_symbol,symbols,current_date)
    LOG.info("Fetched %d good symbols; %d lacking full history: %s", len(rows)-len(missing), len(missing), missing)
    update_screener(build_sheets_client(), rows,
                    scan_utc=now.astimezone(timezone.utc).isoformat(),
                    completed_session=completed_session)
    LOG.info("SUCCESS: refreshed %d S&P100 symbols, completed_session=%s and scan timestamp; G untouched at %s",
             len(rows), completed_session, now.isoformat())
    return True


def scheduled_refresh() -> None:
    """Catch one scheduled failure so the worker remains alive next day."""
    try:
        done=refresh(force=False)
        LOG.info("V2_DAILY_SCHEDULE_COMPLETED refreshed=%s",done)
    except Exception:
        LOG.exception("V2_DAILY_SCHEDULE_FAILED; next daily run remains scheduled")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="Run a single manual check rather than scheduling daily refreshes")
    parser.add_argument("--force", action="store_true",
                        help="Manual check only: bypass calendar and hour guard, retain closed daily bar")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL","INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.force and not args.once:
        parser.error("--force requires --once")
    if args.once:
        result=refresh(force=args.force)
        LOG.info("V2_MANUAL_RUN_COMPLETED refreshed=%s force=%s",result,args.force)
        return
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler=BlockingScheduler(
        timezone=DENVER,
        job_defaults={"coalesce":True,"max_instances":1,"misfire_grace_time":1800})
    scheduler.add_job(scheduled_refresh,"cron",day_of_week="mon-fri",
                      hour=9,minute=0,id="v2_daily_9am_denver",
                      replace_existing=True)
    LOG.info("V2_SCHEDULER_RUNNING daily=09:00 America/Denver, only completed bars; "
             "live positions/order submission=NONE")
    scheduler.start()


if __name__ == "__main__":
    main()