"""Separate Alpaca-wide daily momentum screener.

Scans ALL active, fractionable, tradable US-equity stock symbols (excluding
ETFs/ETNs and core VT/SGOV), scores 252 completed daily bars using the exact
V2 Sheets formula, and publishes to a new Marketwide tab. Never trades.
"""
from __future__ import annotations
import argparse
import logging
import math
import os
import time
from datetime import datetime, timedelta, time as daytime, timezone
from statistics import mean

from marketwide_common import (
    LOG,DENVER,NEW_YORK,DATA_BASE,HEADERS,SHEET_ID,SHEET_TAB,
    assets,auth_headers,get_json,google,last_completed_session,
    metadata,score,session
)

BATCH_SIZE=20
PAGE_LIMIT=8
EXPECTED_HISTORY=252


def historical(s,symbols,today):
    """Complete V2 480-calendar-day, split-adjusted daily bar lookback.

    Multi-symbol bars are sorted symbol-first, so exhaust each page token.
    Every network/API failure invalidates the entire daily snapshot.
    """
    start=(today-timedelta(days=480)).isoformat()
    end=(datetime.combine(today,daytime.min,tzinfo=NEW_YORK)-
         timedelta(seconds=1)).isoformat()
    data={sym:[] for sym in symbols}
    feed=os.getenv("ALPACA_DATA_FEED","iex")
    for offset in range(0,len(symbols),BATCH_SIZE):
        chunk=symbols[offset:offset+BATCH_SIZE]
        params={"symbols":",".join(chunk),"start":start,"end":end,
                "timeframe":"1Day","adjustment":"split","feed":feed,
                "limit":"10000"}
        tokens=set()
        for page in range(PAGE_LIMIT):
            r=get_json(s,DATA_BASE,params=params)
            if not isinstance(r,dict) or not isinstance(r.get("bars"),dict):
                raise RuntimeError("MARKETWIDE_BAR_MAPPING_MISSING")
            for sym,bars in r["bars"].items():
                if sym in data and isinstance(bars,list):
                    data[sym].extend(bars)
            token=r.get("next_page_token")
            if not token:break
            if token in tokens:raise RuntimeError("MARKETWIDE_BAR_TOKEN_LOOP")
            tokens.add(token)
            params["page_token"]=token
        else:raise RuntimeError("MARKETWIDE_BAR_PAGES_EXHAUSTED")
        if (offset//BATCH_SIZE+1)%20==0:
            LOG.info("MARKETWIDE_FETCH_PROGRESS stocks=%d total=%d",
                     min(offset+BATCH_SIZE,len(symbols)),len(symbols))
        # Below Alpaca's free 200 historical requests/minute plan.
        time.sleep(float(os.getenv("MARKETWIDE_PACING_SECONDS",".55")))
    return data


def metrics(bars,today,required):
    parsed={}
    for b in bars:
        try:
            ts=datetime.fromisoformat(str(b["t"]).replace("Z","+00:00"))
            if ts.tzinfo is None:continue
            day=ts.astimezone(NEW_YORK).date()
            if day>=today:continue
            c,h,l,v=[float(b[k]) for k in ("c","h","l","v")]
            if (min(c,h,l)<=0 or v<0 or
                not all(map(math.isfinite,(c,h,l,v)))):continue
            parsed[day]=(c,h,l,v)
        except (KeyError,ValueError,TypeError,OverflowError):
            continue
    items=[(d,*parsed[d]) for d in sorted(parsed)]
    if len(items)<EXPECTED_HISTORY or items[-1][0]!=required:
        return None
    items=items[-EXPECTED_HISTORY:]
    close=items[-1][1]
    sma200=mean(x[1] for x in items[-200:])
    sma50=mean(x[1] for x in items[-50:])
    low52=min(x[3] for x in items)
    high52=max(x[2] for x in items)
    pos52=max(0.,min(1.,(close-low52)/(high52-low52))) if high52>low52 else 0.
    dvol=mean(x[1]*x[4] for x in items[-20:])/1_000_000
    # Use V2's input rounding before passing the same Formula G criteria.
    measures=[round(close,4),round(sma200,4),round(sma50,4),
              round(pos52,4),round(dvol,4)]
    return [*measures,float(score(*measures))]


def prepared_rows(universe,bars,today,completed,scan_utc):
    out=[]
    qualified=0
    full=0
    for sym in sorted(universe):
        result=metrics(bars.get(sym,[]),today,completed)
        if result:
            full+=1
            if result[-1]>=95:qualified+=1
            out.append([sym,*result,completed.isoformat(),scan_utc])
        else:
            out.append([sym,"","","","","",0,completed.isoformat(),scan_utc])
    min_good=max(800,int(len(universe)*.35))
    if full<min_good:
        raise RuntimeError("MARKETWIDE_INSUFFICIENT_252_DAY_COVERAGE "+
                           str(full)+"/"+str(len(universe)))
    # Descending score is stable and makes buyer selection deterministic.
    out.sort(key=lambda row:(-float(row[6]),row[0]))
    return out,{"universe":len(universe),"full_history":full,
                "qualifying_95":qualified,"incomplete":len(universe)-full}


def cell(value):
    if isinstance(value,bool):raise ValueError("Boolean sheet value")
    if isinstance(value,(int,float)):
        if not math.isfinite(value):raise ValueError("Nonfinite sheet number")
        return {"userEnteredValue":{"numberValue":value}}
    return {"userEnteredValue":{"stringValue":str(value)}}


def publish(service,rows,completed,scan_utc):
    sh=service.spreadsheets()
    sheets=metadata(service)
    prop=next((x["properties"] for x in sheets
               if x["properties"]["title"]==SHEET_TAB),None)
    if prop is None:
        r=sh.batchUpdate(spreadsheetId=SHEET_ID,body={"requests":[{
            "addSheet":{"properties":{"title":SHEET_TAB,
                "gridProperties":{"rowCount":max(5000,len(rows)+2),
                                  "columnCount":len(HEADERS),"frozenRowCount":1}}}
            }]}).execute(num_retries=4)
        prop=r["replies"][0]["addSheet"]["properties"]
    else:
        header=sh.values().get(spreadsheetId=SHEET_ID,
            range="'"+SHEET_TAB+"'!A1:I1").execute(num_retries=4).get("values",[[]])[0]
        if header!=HEADERS:
            raise RuntimeError("MARKETWIDE_HEADER_MISMATCH_NO_OVERWRITE")
    count=prop["gridProperties"]["rowCount"]
    requests=[]
    if count<len(rows)+1:
        requests.append({"appendDimension":{"sheetId":prop["sheetId"],
              "dimension":"ROWS","length":len(rows)+1-count}})
        count=len(rows)+1
    requests.extend([
       {"updateCells":{"range":{"sheetId":prop["sheetId"],
          "startRowIndex":0,"endRowIndex":count,"startColumnIndex":0,
          "endColumnIndex":len(HEADERS)},"rows":[],"fields":"userEnteredValue"}},
       {"updateCells":{"start":{"sheetId":prop["sheetId"],"rowIndex":0,"columnIndex":0},
          "rows":[{"values":[cell(v) for v in HEADERS]}]+[
                     {"values":[cell(v) for v in row]} for row in rows],
          "fields":"userEnteredValue"}}
    ])
    sh.batchUpdate(spreadsheetId=SHEET_ID,body={"requests":requests}).execute(num_retries=4)
    # Full readback on the specific owned tab; no claim of success without it.
    raw=sh.values().get(spreadsheetId=SHEET_ID,
        range="'"+SHEET_TAB+"'!A1:I"+str(len(rows)+1)).execute(num_retries=4)
    actual=raw.get("values",[])
    if (len(actual)!=len(rows)+1 or actual[0]!=HEADERS or
        any(len(r)<9 or str(r[7])!=completed.isoformat() or
            str(r[8])!=scan_utc for r in actual[1:])):
        raise RuntimeError("MARKETWIDE_PUBLISH_READBACK_FAILED")
    for i in (0,len(rows)//2,len(rows)-1):
        if str(actual[i+1][0])!=str(rows[i][0]):
            raise RuntimeError("MARKETWIDE_PUBLISH_SYMBOL_READBACK_FAILED")
    return True


def run_once(s=None,now=None):
    now=now or datetime.now(DENVER)
    today=now.astimezone(NEW_YORK).date()
    s=s or session()
    if today.weekday()>=5:
        LOG.info("MARKETWIDE_SKIP_WEEKEND");return False
    required=last_completed_session(s,today)
    universe=assets(s)
    symbols=sorted(universe)
    allbars=historical(s,symbols,today)
    stamp=now.astimezone(timezone.utc).isoformat()
    rows,stats=prepared_rows(universe,allbars,today,required,stamp)
    publish(google(),rows,required,stamp)
    LOG.warning("MARKETWIDE_SCREEN_COMPLETE %s completed_session=%s "
                "scan_utc=%s readback=PASS",stats,required,stamp)
    return True


def scheduled_run():
    try:run_once()
    except Exception:
        LOG.exception("MARKETWIDE_SCREEN_FAILED; last-good sheet preserved when prevalidation fails")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),
          format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.once:
        run_once()
        return
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler=BlockingScheduler(timezone=DENVER,job_defaults={
         "coalesce":True,"max_instances":1,"misfire_grace_time":3600})
    scheduler.add_job(scheduled_run,"cron",day_of_week="mon-fri",
         hour=7,minute=0,id="marketwide_screen_daily",replace_existing=True)
    LOG.warning("MARKETWIDE_SCREENER_STARTED weekdays=07:00_America/Denver "
                "current_day_bar_excluded=TRUE orders=DISABLED")
    if os.getenv("MARKETWIDE_SCAN_ON_BOOT","").lower() in ("1","true"):
        scheduled_run()
    scheduler.start()


if __name__=="__main__":
    main()
