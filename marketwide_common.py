"""Isolated Alpaca-wide strategy common rules.

The V2 score below is a faithful Python port of the Google Sheets
Screener!G LET formula. Do not change it without benchmarking V2.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG=logging.getLogger("marketwide")
DENVER=ZoneInfo("America/Denver")
NEW_YORK=ZoneInfo("America/New_York")
TRADE_BASE=os.getenv("ALPACA_TRADING_BASE_URL","https://api.alpaca.markets").rstrip("/")
DATA_BASE="https://data.alpaca.markets/v2/stocks/bars"
SHEET_ID=os.getenv("GOOGLE_SHEET_ID","14-Fvk8YruHeGB95lyrWtjj-KfQ-AkIjSyAf5gQdHDFQ")
SHEET_TAB="Marketwide"
HEADERS=["symbol","close","sma_200","sma_50","pos_52w",
         "dollar_vol_m","score","completed_session_date","scan_utc"]
SCORE_MIN=Decimal("95")
RESERVED=frozenset({"VT","SGOV"})
STOCK_PATTERN=re.compile(r"^[A-Z][A-Z0-9.\-]{0,11}$")
NON_STOCK_NAME=re.compile(r"\b(?:ETF|ETN|EXCHANGE.TRADED(?: FUND| NOTE)?|INDEX FUND)\b",re.I)
PERCENT=Decimal(".001")


def auth_headers():
    key=os.getenv("APCA_API_KEY_ID","")
    secret=os.getenv("APCA_API_SECRET_KEY","")
    if not key or not secret:raise RuntimeError("ALPACA_CREDENTIALS_MISSING")
    return {"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":secret}


def session():
    s=requests.Session()
    retry=Retry(total=5,backoff_factor=1.5,
                status_forcelist=[429,500,502,503,504],
                allowed_methods=["GET"],respect_retry_after_header=True)
    s.mount("https://",HTTPAdapter(max_retries=retry))
    s.headers.update({"Accept":"application/json","User-Agent":"MarketwideMomentum/1.0"})
    return s


def get_json(s,url,**kwargs):
    r=s.get(url,headers=auth_headers(),timeout=(15,90),**kwargs)
    r.raise_for_status()
    return r.json()


def score(close,sma200,sma50,pos52,dollar_vol_m):
    """Exact V2 score shape and 95+ gate; all input units as V2."""
    try:
        b,c,d,e,f=map(float,[close,sma200,sma50,pos52,dollar_vol_m])
        if not all(math.isfinite(v) for v in [b,c,d,e,f]) or min(b,c,d)<=0 or f<10:
            return Decimal(0)
        gap50=b/d-1
        gap200=b/c-1
        if not (b>c and d>c and 0<=gap50<=.25 and gap200<=.6 and e>=.75):
            return Decimal(0)
        a=max(0.,min(1.,gap50/.05,(.25-gap50)/.1))
        z=max(0.,min(1.,gap200/.1,(.6-gap200)/.15))
        y=max(0.,min(1.,(e-.75)/.2,(1-e)/.015))
        q=min(1.,math.log(1+f)/math.log(101))
        v=10+35*a+20*z+25*y+10*q
        return Decimal(str(v)).quantize(Decimal(".0001"),rounding=ROUND_HALF_UP)
    except (ValueError,TypeError,ZeroDivisionError,OverflowError):
        return Decimal(0)


def eligible_asset(a):
    """US equity stock only; Alpaca's US equity class also includes ETFs."""
    if not isinstance(a,dict):return False
    symbol=str(a.get("symbol","")).upper().strip()
    cls=a.get("class",a.get("asset_class"))
    if (cls!="us_equity" or a.get("status")!="active" or
        not a.get("tradable") or not a.get("fractionable") or
        not STOCK_PATTERN.fullmatch(symbol) or symbol in RESERVED):
        return False
    return not NON_STOCK_NAME.search(str(a.get("name","")))


def assets(s):
    data=get_json(s,TRADE_BASE+"/v2/assets",
                  params={"status":"active","asset_class":"us_equity"})
    if not isinstance(data,list) or len(data)<1000:
        raise RuntimeError("MARKETWIDE_ASSET_LIST_INCOMPLETE")
    out={}
    for a in data:
        if eligible_asset(a):
            sym=a["symbol"].upper()
            if sym in out:raise RuntimeError("DUPLICATE_ALPACA_ASSET "+sym)
            out[sym]=a
    if len(out)<1000:
        raise RuntimeError("MARKETWIDE_ELIGIBLE_STOCKS_TOO_FEW")
    LOG.info("MARKETWIDE_UNIVERSE total_assets=%d active_fractionable_stocks=%d",
             len(data),len(out))
    return out


def last_completed_session(s,today):
    if today.weekday()>=5:raise RuntimeError("MARKET_CLOSED_WEEKEND")
    beginning=today-timedelta(days=16)
    data=get_json(s,TRADE_BASE+"/v2/calendar",
        params={"start":beginning.isoformat(),"end":today.isoformat()})
    if not isinstance(data,list):raise RuntimeError("ALPACA_CALENDAR_UNVERIFIABLE")
    dates=[]
    for x in data:
        if isinstance(x,dict) and x.get("date"):
            v=date.fromisoformat(x["date"])
            if v<today:dates.append(v)
    if not dates:raise RuntimeError("NO_COMPLETED_TRADING_SESSION")
    return max(dates)


def market_open(s):
    r=get_json(s,TRADE_BASE+"/v2/clock")
    if not isinstance(r,dict) or not isinstance(r.get("is_open"),bool):
        raise RuntimeError("BROKER_MARKET_CLOCK_UNVERIFIABLE")
    return r["is_open"]


def google():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    raw=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON","")
    if not raw:raise RuntimeError("GOOGLE_CREDENTIALS_MISSING")
    info=json.loads(raw)
    creds=service_account.Credentials.from_service_account_info(
        info,scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets","v4",credentials=creds,cache_discovery=False)


def metadata(service):
    return service.spreadsheets().get(spreadsheetId=SHEET_ID,
          fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
          ).execute(num_retries=4)["sheets"]


def allowed_start(s):
    now=datetime.now(DENVER)
    if now.weekday()>=5:return False
    return market_open(s)


def round_notional(available):
    amt=Decimal(str(available))
    if not amt.is_finite() or amt<0:raise ValueError("INVALID_MARGIN_POWER")
    return (amt*PERCENT).quantize(Decimal(".01"),rounding=ROUND_DOWN)
