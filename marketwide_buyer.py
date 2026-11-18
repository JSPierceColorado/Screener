"""Separate 0.1%-of-margin, top-10/day market-wide V2-score buyer.

Uses ONLY fresh, fully published Marketwide scores (>=95), excludes V2's
S&P100 ownership universe, held positions, open orders and protected core.
Reuses existing V2 Seller (profit-only for all other symbols). Durable
pre-POST ledger prevents blind duplicate orders on unknown API responses.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import requests
import sqlite3
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from contextlib import closing
from urllib.parse import quote

from marketwide_common import (
    LOG,DENVER,NEW_YORK,HEADERS,SHEET_ID,SHEET_TAB,RESERVED,SCORE_MIN,
    TRADE_BASE,STOCK_PATTERN,auth_headers,get_json,google,last_completed_session,
    market_open,round_notional,session
)

DB_PATH="/data/marketwide_buyer.sqlite3"
MAX_BUYS_PER_DAY=10
MAX_TOTAL_POSITIONS=150  # Other bots' positions count toward this overall cap.
MIN_NOTIONAL=Decimal("1.00")


def connection(path=DB_PATH):
    os.makedirs(os.path.dirname(os.path.abspath(path)),exist_ok=True)
    c=sqlite3.connect(path,timeout=30)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        session TEXT NOT NULL,symbol TEXT NOT NULL,cid TEXT NOT NULL UNIQUE,
        amount TEXT NOT NULL,state TEXT NOT NULL,
        broker_order_id TEXT NOT NULL DEFAULT '',
        score TEXT NOT NULL,observed_utc TEXT NOT NULL,
        PRIMARY KEY(session,symbol))""")
    c.commit()
    return c


def client_id(day,symbol):
    clean=symbol.replace(".","dot").replace("-","dash")
    if not STOCK_PATTERN.fullmatch(symbol):raise ValueError("INVALID_TICKER")
    return "mwbu-"+day.replace("-","")+"-"+clean


def data_rows(values,completed,today):
    if not isinstance(values,list) or not values or values[0]!=HEADERS:
        raise RuntimeError("MARKETWIDE_SHEET_HEADERS_UNVERIFIABLE")
    rows=values[1:]
    if len(rows)<1000:raise RuntimeError("MARKETWIDE_SNAPSHOT_TOO_SMALL")
    seen=set()
    good=0
    scans=set()
    selected=[]
    previous_score=float("inf")
    for row in rows:
        if not isinstance(row,list) or len(row)<9:
            raise RuntimeError("MARKETWIDE_SNAPSHOT_ROW_INCOMPLETE")
        sym=str(row[0]).upper()
        if sym in seen or not STOCK_PATTERN.fullmatch(sym):
            raise RuntimeError("MARKETWIDE_SYMBOLS_DUPLICATED_OR_INVALID")
        seen.add(sym)
        if str(row[7])!=completed.isoformat():
            raise RuntimeError("MARKETWIDE_COMPLETED_SESSION_STALE")
        scans.add(str(row[8]))
        try:
            current=datetime.fromisoformat(str(row[8]).replace("Z","+00:00"))
        except ValueError:raise RuntimeError("MARKETWIDE_SCAN_TIME_UNVERIFIABLE")
        if current.tzinfo is None or current.astimezone(DENVER).date()!=today:
            raise RuntimeError("MARKETWIDE_NO_TODAY_SCAN")
        try:s=float(row[6])
        except (TypeError,ValueError):raise RuntimeError("MARKETWIDE_SCORE_INVALID")
        if not 0<=s<=100:raise RuntimeError("MARKETWIDE_SCORE_OUT_OF_BOUNDS")
        if s>previous_score+0.0001:
            raise RuntimeError("MARKETWIDE_SCORE_ORDER_INVALID")
        previous_score=s
        if row[1] not in ("",None):good+=1
        if s>=float(SCORE_MIN):
            selected.append((sym,Decimal(str(s))))
    if len(scans)!=1:raise RuntimeError("MARKETWIDE_MULTIPLE_SCANS_MIXED")
    if good<max(800,int(len(rows)*.35)):
        raise RuntimeError("MARKETWIDE_INCOMPLETE_HISTORY_COVERAGE")
    return selected,{"universe":len(rows),"full_history":good,
                     "qualifying":len(selected),"scan_utc":next(iter(scans))}


def read_sheet(service,completed,today):
    sh=service.spreadsheets()
    meta=sh.get(spreadsheetId=SHEET_ID,fields=
          "sheets(properties(title,gridProperties(rowCount,columnCount)))"
          ).execute(num_retries=4)
    props={x["properties"]["title"]:x["properties"]
           for x in meta.get("sheets",[]) if x.get("properties")}
    market_count=int(props.get(SHEET_TAB,{}).get("gridProperties",{}).get("rowCount",0))
    v2_count=int(props.get("Screener",{}).get("gridProperties",{}).get("rowCount",0))
    if not 1000<=market_count<=25000 or not 95<=v2_count<=150:
        raise RuntimeError("MARKETWIDE_OR_V2_SHEET_BOUNDS_UNVERIFIABLE")
    values=sh.values()
    # Same existing V2 workbook; separate tab for the new strategy.
    mw=values.get(spreadsheetId=SHEET_ID,
          range="'"+SHEET_TAB+"'!A1:I"+str(market_count)
         ).execute(num_retries=4).get("values",[])
    selected,summary=data_rows(mw,completed,today)
    ref=values.get(spreadsheetId=SHEET_ID,
          range="'Screener'!A1:J"+str(v2_count)
         ).execute(num_retries=4).get("values",[])
    if not ref or [str(x).lower() for x in ref[0][:7]]!=HEADERS[:7]:
        raise RuntimeError("V2_SCREENER_SCHEMA_UNVERIFIABLE")
    if len(ref)<95:
        raise RuntimeError("V2_CONSTITUENTS_INCOMPLETE")
    members=set()
    for row in ref[1:]:
        if not row:continue
        sym=str(row[0]).upper().strip()
        if not STOCK_PATTERN.fullmatch(sym):raise RuntimeError("V2_CONSTITUENTS_INVALID")
        members.add(sym)
    if not 95<=len(members)<=110:raise RuntimeError("V2_CONSTITUENTS_INCOMPLETE")
    if len(ref[1])<10 or str(ref[1][9])!=completed.isoformat():
        raise RuntimeError("V2_SCREENER_STALE_CONSTITUENTS")
    return [(sym,score) for sym,score in selected if sym not in members and sym not in RESERVED],summary,{
        "excluded_v2":sum(1 for sym,_ in selected if sym in members),
        "excluded_core":sum(1 for sym,_ in selected if sym in RESERVED)}


def broker_get(s,path,**kwargs):
    return get_json(s,TRADE_BASE+path,**kwargs)


def broker_order_by_cid(s,cid):
    r=s.get(TRADE_BASE+"/v2/orders:by_client_order_id",
            params={"client_order_id":cid},
            headers=auth_headers(),timeout=(15,30))
    if r.status_code==404:return None
    r.raise_for_status()
    return r.json()


def reconcile(c,s):
    pending=c.execute("""SELECT * FROM orders
         WHERE state IN ('RESERVED','SUBMITTED','OPEN')""").fetchall()
    uncertain=0
    for row in pending:
        try:
            item=broker_order_by_cid(s,row["cid"])
        except Exception as exc:
            LOG.error("MARKETWIDE_ORDER_LOOKUP_UNAVAILABLE cid=%s reason=%s",
                      row["cid"],type(exc).__name__)
            uncertain+=1
            continue
        if item is None:
            # Uncertain POST must not be replayed, including across a restart.
            uncertain+=1
            LOG.warning("MARKETWIDE_ORDER_UNRESOLVED cid=%s no_retry=TRUE",row["cid"])
            continue
        state=str(item.get("status","")).lower()
        if (str(item.get("client_order_id"))!=row["cid"] or
            str(item.get("symbol","")).upper()!=row["symbol"] or
            str(item.get("side","")).lower()!="buy"):
            uncertain+=1
            continue
        if state in ("filled",): status="FILLED"
        elif state in ("canceled","expired","rejected","done_for_day"):
            status="FINAL_UNFILLED"
        elif state in ("new","accepted","partially_filled","pending_new",
                      "pending_cancel","accepted_for_bidding","calculated"):
            status="OPEN"
        else:
            uncertain+=1
            continue
        with c:
            c.execute("""UPDATE orders SET state=?,broker_order_id=?
                WHERE cid=?""",(status,str(item.get("id","")),row["cid"]))
    return uncertain


def held_symbols(s):
    p=broker_get(s,"/v2/positions")
    if not isinstance(p,list):raise RuntimeError("POSITION_SNAPSHOT_UNVERIFIABLE")
    return {str(x["symbol"]).upper() for x in p if isinstance(x,dict) and x.get("symbol")}


def open_orders(s):
    data=broker_get(s,"/v2/orders",params={"status":"open","limit":500,"nested":"true"})
    if not isinstance(data,list) or len(data)>=500:
        raise RuntimeError("OPEN_ORDER_HISTORY_UNVERIFIABLE")
    return {str(x["symbol"]).upper() for x in data if isinstance(x,dict) and x.get("symbol")}


def weekly_stops(s,now):
    day=now.astimezone(NEW_YORK).date()
    monday=day-timedelta(days=day.weekday())
    data=broker_get(s,"/v2/orders",params={"status":"all","side":"sell","limit":500,
               "direction":"desc","after":monday.isoformat()+"T00:00:00Z"})
    if not isinstance(data,list) or len(data)>=500:
        raise RuntimeError("WEEKLY_STOP_HISTORY_UNVERIFIABLE")
    found=set()
    for order in data:
        cid=str(order.get("client_order_id","")).lower()
        if not cid.startswith("v2sl-"):continue
        try:
            dt=datetime.strptime(cid[5:13],"%Y%m%d").date()
        except ValueError:continue
        if monday<=dt<=day and Decimal(str(order.get("filled_qty","0")))>0:
            sym=str(order.get("symbol","")).upper()
            if STOCK_PATTERN.fullmatch(sym):found.add(sym)
    return found


def test_live_permission():
    if os.getenv("DRY_RUN","true").lower() not in ("false","0","no"):
        return False
    if (os.getenv("ENABLE_LIVE_TRADING","false").lower()!="true" or
        os.getenv("LIVE_TRADING_ACK")!="I_ACCEPT_REAL_MONEY_BUYS" or
        os.getenv("ALPACA_PAPER","false").lower() in ("true","1","yes")):
        raise RuntimeError("MARKETWIDE_LIVE_MODE_NOT_AUTHORIZED")
    if TRADE_BASE!="https://api.alpaca.markets":
        raise RuntimeError("MARKETWIDE_LIVE_BROKER_BASE_MISMATCH")
    return True


def run_once(*,s=None,service=None,now=None,path=DB_PATH):
    now=now or datetime.now(DENVER)
    day=now.astimezone(DENVER).date()
    s=s or session()
    if day.weekday()>=5 or not market_open(s):
        LOG.info("MARKETWIDE_BUY_MARKET_CLOSED");return {"status":"CLOSED","orders":0}
    live=test_live_permission()
    completed=last_completed_session(s,now.astimezone(NEW_YORK).date())
    score_rows,stats,excluded=read_sheet(service or google(),completed,day)
    LOG.info("MARKETWIDE_BUY_SIGNALS completed=%s stats=%s excluded=%s",
             completed,stats,excluded)
    with closing(connection(path)) as c:
        uncertain=reconcile(c,s)
        if uncertain:
            LOG.error("MARKETWIDE_RECONCILIATION_REQUIRED pending=%d no_new_orders=TRUE",uncertain)
            return {"status":"RECONCILIATION_REQUIRED","orders":0}
        used=c.execute("SELECT COUNT(*) count FROM orders WHERE session=?",(day.isoformat(),)).fetchone()["count"]
        room=max(0,MAX_BUYS_PER_DAY-used)
        if room==0:return {"status":"DAILY_CAP_REACHED","orders":0}
        held=held_symbols(s)
        outstanding=open_orders(s)
        # No-stop-loss holdings can accumulate indefinitely; prevent runaway
        # portfolio growth while still allowing more opportunities.
        total_reserved=len(held|outstanding)
        room=min(room,max(0,MAX_TOTAL_POSITIONS-total_reserved))
        if room<=0:
            LOG.warning("MARKETWIDE_GLOBAL_POSITION_CAP_REACHED held_or_open=%d cap=%d",
                        total_reserved,MAX_TOTAL_POSITIONS)
            return {"status":"PORTFOLIO_CAP_REACHED","orders":0}
        stopped=weekly_stops(s,now)
        account=broker_get(s,"/v2/account")
        if account.get("trading_blocked") or account.get("account_blocked"):
            raise RuntimeError("ALPACA_ACCOUNT_BLOCKED")
        buying_power=Decimal(str(account.get("buying_power","NaN")))
        if not buying_power.is_finite() or buying_power<0:
            raise RuntimeError("MARGIN_BUYING_POWER_INVALID")
        initial_power=buying_power
        placed=0
        outcomes={}
        for sym,sc in score_rows:
            if placed>=room:break
            if sym in held:outcomes[sym]="HELD";continue
            if sym in outstanding:outcomes[sym]="OPEN_ORDER";continue
            if sym in stopped:outcomes[sym]="HISTORICAL_STOP_COOLDOWN";continue
            cid=client_id(day.isoformat(),sym)
            if c.execute("SELECT 1 FROM orders WHERE cid=?",(cid,)).fetchone():
                outcomes[sym]="PRIOR_ORDER";continue
            existing=broker_order_by_cid(s,cid)
            if existing is not None:
                outcomes[sym]="PREVIOUS_BROKER_ORDER"
                with c:
                    c.execute("""INSERT OR IGNORE INTO orders(session,symbol,cid,amount,state,score,observed_utc)
                        VALUES(?,?,?,?,?,?,?)""",(day.isoformat(),sym,cid,"0","SUBMITTED",str(sc),now.isoformat()))
                continue
            asset=broker_get(s,"/v2/assets/"+quote(sym,safe=""))
            if (not isinstance(asset,dict) or asset.get("status")!="active" or
                asset.get("class")!="us_equity" or not asset.get("tradable") or
                not asset.get("fractionable") or not asset.get("marginable")):
                outcomes[sym]="NOT_MARGINABLE_FRACTIONABLE_OR_TRADABLE"
                continue
            try:broker_get(s,"/v2/positions/"+quote(sym,safe=""))
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code!=404:raise
            else:
                outcomes[sym]="BECAME_HELD"
                continue
            latest=broker_get(s,"/v2/account")
            latest_power=Decimal(str(latest.get("buying_power","NaN")))
            if (not latest_power.is_finite() or latest_power<0 or
                latest.get("trading_blocked") or latest.get("account_blocked")):
                raise RuntimeError("BROKER_MARGIN_BALANCE_UNVERIFIABLE")
            buying_power=min(buying_power,latest_power)
            amount=round_notional(buying_power)
            if amount<MIN_NOTIONAL:
                outcomes[sym]="BELOW_MINIMUM_MARGIN_POWER"
                break
            if not live:
                outcomes[sym]="DRY_RUN"
                LOG.warning("MARKETWIDE_DRY_RUN_PREVIEW sym=%s score=%s notional=%s",
                            sym,sc,amount)
                placed+=1
                buying_power-=amount
                continue
            # A durable RESERVED row is written BEFORE a single POST. Any
            # uncertainty freezes further buys until broker reconciliation.
            with c:
                c.execute("""INSERT INTO orders(session,symbol,cid,amount,state,score,observed_utc)
                    VALUES(?,?,?,?,?,?,?)""",(
                    day.isoformat(),sym,cid,str(amount),"RESERVED",str(sc),now.isoformat()))
            payload={"symbol":sym,"side":"buy","type":"market",
                     "notional":str(amount),"time_in_force":"day",
                     "client_order_id":cid}
            try:
                resp=s.post(TRADE_BASE+"/v2/orders",json=payload,
                     headers=auth_headers(),timeout=(15,35))
                resp.raise_for_status()
                order=resp.json()
                if order.get("client_order_id")!=cid:
                    raise RuntimeError("BROKER_ORDER_ID_MISMATCH")
            except Exception:
                LOG.exception("MARKETWIDE_POST_UNCERTAIN cid=%s no_retry=TRUE",cid)
                return {"status":"BROKER_ORDER_UNCERTAIN","orders":placed}
            with c:
                c.execute("UPDATE orders SET state='SUBMITTED',broker_order_id=? WHERE cid=?",
                          (str(order.get("id","")),cid))
            outcomes[sym]="SUBMITTED_PENDING_FILL"
            LOG.warning("MARKETWIDE_BUY_SUBMITTED symbol=%s score=%s amount=%s cid=%s",
                        sym,sc,amount,cid)
            placed+=1
            buying_power-=amount
            held.add(sym)
        LOG.warning("MARKETWIDE_BUY_AUDIT date=%s live=%s available_start=%s "
                    "eligible_non_v2=%d orders=%d prior_daily=%d outcomes=%s",
                    day,live,initial_power,len(score_rows),placed,used,
                    json.dumps(outcomes,sort_keys=True))
        return {"status":"SUBMITTED" if live else "PREVIEW",
                "orders":placed,"qualified":len(score_rows),"outcomes":outcomes}


def scheduled_run():
    try:
        run_once()
    except Exception:
        LOG.exception("MARKETWIDE_BUYER_FAILED; no blind retry on uncertain broker POST")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),
         format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with closing(connection()) as c:
        LOG.info("MARKETWIDE_BUYER_LEDGER_READY path=%s historical_rows=%d",
                 DB_PATH,c.execute("SELECT COUNT(*) FROM orders").fetchone()[0])
    if os.getenv("MARKETWIDE_DIAGNOSTIC_ON_START","").lower() in ("true","1"):
        try:
            s=session()
            account=broker_get(s,"/v2/account")
            today=datetime.now(DENVER).date()
            completed=last_completed_session(s,datetime.now(NEW_YORK).date())
            picked,stats,excluded=read_sheet(google(),completed,today)
            LOG.warning("MARKETWIDE_BUYER_DIAGNOSTIC account_status=%s "
                "score_session=%s stats=%s excluded=%s eligible_non_v2=%d "
                "sample_symbols=%s market_open=%s live=%s NO_ORDERS=TRUE",
                account.get("status"),completed,stats,excluded,len(picked),
                [sym for sym,_ in picked[:12]],market_open(s),
                test_live_permission())
        except Exception:
            LOG.exception("MARKETWIDE_BUYER_DIAGNOSTIC_FAILED; "
                          "startup never sends any orders")
    if args.once:
        scheduled_run();return
    from apscheduler.schedulers.blocking import BlockingScheduler
    scheduler=BlockingScheduler(timezone=DENVER,job_defaults={
          "coalesce":True,"max_instances":1,"misfire_grace_time":900})
    scheduler.add_job(scheduled_run,"cron",day_of_week="mon-fri",
         hour=11,minute="0,15,30,45",id="marketwide_buy_11am",
         replace_existing=True)
    LOG.warning("MARKETWIDE_BUYER_STARTED 11:00-11:45 Denver "
                "minimum_score=95 fraction=0.1pct_rem_margin max_daily=10 portfolio_max=150 "
                "excluded_v2_sp100=true seller=EXISTING_V2_PROFIT_ONLY live=%s",
                test_live_permission())
    scheduler.start()


if __name__=="__main__":main()
