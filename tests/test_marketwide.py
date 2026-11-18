"""Offline regression for separate marketwide scoring and buyer.

Never talk to Alpaca or Google during tests; no user orders.
"""
from datetime import date,datetime,timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import marketwide_common as common
import marketwide_screener as screen
import marketwide_buyer as buyer

DAY=datetime(2026,11,19,11,5,tzinfo=common.DENVER)
PREV=date(2026,11,18)
STAMP=DAY.astimezone(timezone.utc).isoformat()


def asset(sym,name="A Sample Corporation",**kwargs):
    return {"symbol":sym,"name":name,"class":"us_equity","status":"active",
            "tradable":True,"fractionable":True,"marginable":True,**kwargs}


def synthetic_bars(days=252,completed=PREV,close=110.):
    from datetime import timedelta
    start=completed-timedelta(days=days-1)
    result=[]
    for i in range(days):
        d=start+timedelta(days=i)
        result.append({"t":d.isoformat()+"T05:00:00Z",
                       "c":close,"h":120,"l":80,"v":2_000_000})
    return result


def synthetic_sheet(n=1200,hit="ZZZZ"):
    rows=[list(common.HEADERS)]
    for i in range(n):
        sym=hit if i==0 else f"TEST{i:05d}"
        score=98 if i==0 else 0
        rows.append([sym,100,95,97,.9,15,score,PREV.isoformat(),STAMP])
    return rows


def test_real_v2_sheet_score_matches_abby_and_apple():
    # Real observed original Google Sheet formula outputs Nov 18, 2026.
    assert abs(common.score(351.91,302.1289,340.4063,.924,16716.2811)
               -Decimal("85.4058"))<Decimal(".0002")
    assert abs(common.score(292.82,241.4812,277.1476,.9099,1499.6432)
               -Decimal("94.9875"))<Decimal(".0002")
    assert common.score(100,80,110,.9,1000)==0


def test_asset_filter_is_full_alpaca_stocks_not_etfs():
    assert common.eligible_asset(asset("NVDA"))
    assert common.eligible_asset(asset("REIT","Alpha Property Trust"))
    assert not common.eligible_asset(asset("VT"))
    assert not common.eligible_asset(asset("SGOV"))
    assert not common.eligible_asset(asset("TQQQ","ProShares UltraPro QQQ ETF"))
    assert not common.eligible_asset(asset("QQQ","Invesco Exchange-Traded Fund"))
    assert not common.eligible_asset({**asset("OTC"),"tradable":False})
    assert common.eligible_asset({**asset("PENNY"),"fractionable":False})
    # Nonfractionable stocks are screened, but the fractional-notional BUYER
    # rejects them unless whole-share sizing is explicitly implemented.
    assert not common.eligible_asset({**asset("BTC"),"class":"crypto"})


def test_252_daily_completed_bars_exclude_live_partial():
    rows=synthetic_bars()
    rows.append({"t":"2026-11-19T05:00:00Z","c":500,"h":500,"l":500,"v":1000000})
    m=screen.metrics(rows,date(2026,11,19),PREV)
    assert m is not None
    assert m[0]==110
    assert len(m)==6
    assert m[-1]==float(common.score(*m[:5]))
    assert screen.metrics(rows[:251],date(2026,11,19),PREV) is None
    assert screen.metrics(rows[:-1],date(2026,11,20),
                          date(2026,11,19)) is None


def test_published_rows_cover_entire_universe_and_score_sorted():
    universe={f"T{i:04}":asset(f"T{i:04}") for i in range(1000)}
    bars={sym:synthetic_bars() for sym in universe}
    rows,meta=screen.prepared_rows(universe,bars,date(2026,11,19),
                                   PREV,STAMP)
    assert len(rows)==1000
    assert meta["universe"]==1000
    assert meta["full_history"]==1000
    assert all(r[7]==PREV.isoformat() and r[8]==STAMP for r in rows)
    assert all(rows[i][6]>=rows[i+1][6] for i in range(len(rows)-1))


def test_insufficient_252_bar_coverage_prevents_publication():
    universe={f"T{i:04}":asset(f"T{i:04}") for i in range(1000)}
    bars={sym:synthetic_bars() for sym in list(universe)[:100]}
    with pytest.raises(RuntimeError,match="INSUFFICIENT_252_DAY_COVERAGE"):
        screen.prepared_rows(universe,bars,date(2026,11,19),PREV,STAMP)


def test_multi_symbol_pagination_respected(monkeypatch):
    monkeypatch.setenv("MARKETWIDE_PACING_SECONDS","0")
    monkeypatch.setattr(screen.time,"sleep",lambda n:None)
    pages=[{"bars":{"AAPL":[{"t":"2026-11-18T05:00:00Z"}]},
             "next_page_token":"NEXT"},
           {"bars":{"AAPL":[{"t":"2026-11-17T05:00:00Z"}]},
             "next_page_token":None}]
    monkeypatch.setattr(screen,"get_json",lambda *args,**kwargs:pages.pop(0))
    got=screen.historical(object(),["AAPL"],date(2026,11,19))
    assert len(got["AAPL"])==2


def test_dedup_and_dated_snapshot_denies_mixed_days():
    rows=synthetic_sheet()
    selected,report=buyer.data_rows(rows,PREV,DAY.date())
    assert selected==[("ZZZZ",Decimal("98"))]
    assert report["universe"]==1200
    rows[2][7]="2026-11-17"
    with pytest.raises(RuntimeError,match="STALE"):
        buyer.data_rows(rows,PREV,DAY.date())
    rows=synthetic_sheet()
    rows[2][0]="ZZZZ"
    with pytest.raises(RuntimeError,match="DUPLICATED"):
        buyer.data_rows(rows,PREV,DAY.date())


def test_buyer_requires_current_day_completed_scan():
    rows=synthetic_sheet()
    rows[1][8]=datetime(2026,11,17,tzinfo=timezone.utc).isoformat()
    with pytest.raises(RuntimeError,match="NO_TODAY_SCAN"):
        buyer.data_rows(rows,PREV,DAY.date())


def test_sizing_exact_margin_fraction_and_minimum():
    assert common.round_notional(10000)==Decimal("10.00")
    assert common.round_notional(12345.67)==Decimal("12.34")
    assert common.round_notional(999)==Decimal("0.99")
    with pytest.raises(ValueError):
        common.round_notional(-1)


def test_buyer_live_permission_fail_closed(monkeypatch):
    monkeypatch.delenv("ENABLE_LIVE_TRADING",raising=False)
    monkeypatch.setenv("DRY_RUN","false")
    with pytest.raises(RuntimeError,match="NOT_AUTHORIZED"):
        buyer.test_live_permission()
    monkeypatch.setenv("DRY_RUN","true")
    assert buyer.test_live_permission() is False


def test_deterministic_strategy_client_order_ids():
    assert buyer.client_id("2026-11-19","BRK.B")=="mwbu-20261119-BRKdotB"
    assert buyer.client_id("2026-11-19","TTMI")=="mwbu-20261119-TTMI"


def test_reconcile_unknown_post_freezes_and_does_not_repeat(monkeypatch,tmp_path):
    db=str(tmp_path/"orders.sqlite3")
    with buyer.connection(db) as c:
        c.execute("INSERT INTO orders(session,symbol,cid,amount,state,score,observed_utc) VALUES(?,?,?,?,?,?,?)",
                  ("2026-11-19","AAPL","mwbu-20261119-AAPL","10","RESERVED","95",STAMP))
        c.commit()
        monkeypatch.setattr(buyer,"broker_order_by_cid",lambda *a:None)
        assert buyer.reconcile(c,object())==1
        assert c.execute("SELECT state FROM orders").fetchone()[0]=="RESERVED"


def test_held_and_v2_universe_dont_become_marketwide_entries(monkeypatch,tmp_path):
    # Mock the APIs; even a good score must not purchase an existing holding.
    monkeypatch.setattr(buyer,"market_open",lambda *a:True)
    monkeypatch.setattr(buyer,"last_completed_session",lambda *a:PREV)
    monkeypatch.setattr(buyer,"read_sheet",lambda *a:(
        [("AAPL",Decimal("99")),("TTMI",Decimal("98"))],
        {"universe":1500},{}))
    monkeypatch.setattr(buyer,"held_symbols",lambda *a:{"AAPL","TTMI"})
    monkeypatch.setattr(buyer,"open_orders",lambda *a:set())
    monkeypatch.setattr(buyer,"weekly_stops",lambda *a:set())
    monkeypatch.setattr(buyer,"broker_order_by_cid",lambda *a:None)
    monkeypatch.setattr(buyer,"broker_get",lambda *a,**k:{"buying_power":"20000"})
    monkeypatch.setenv("DRY_RUN","true")
    result=buyer.run_once(s=object(),service=object(),now=DAY,
                          path=str(tmp_path/"ledger.sqlite3"))
    assert result["orders"]==0
    assert result["outcomes"]["AAPL"]=="HELD"
    assert result["outcomes"]["TTMI"]=="HELD"


def test_day_limit_never_exceeds_ten(monkeypatch,tmp_path):
    monkeypatch.setattr(buyer,"market_open",lambda *a:True)
    monkeypatch.setattr(buyer,"last_completed_session",lambda *a:PREV)
    monkeypatch.setattr(buyer,"read_sheet",lambda *a:(
        [(f"C{i:02}",Decimal("97")) for i in range(15)],{"universe":1500},{}))
    monkeypatch.setattr(buyer,"held_symbols",lambda *a:set())
    monkeypatch.setattr(buyer,"open_orders",lambda *a:set())
    monkeypatch.setattr(buyer,"weekly_stops",lambda *a:set())
    monkeypatch.setattr(buyer,"broker_order_by_cid",lambda *a:None)
    def get(_,path,**kwargs):
        if path.startswith("/v2/assets/"):
            return asset("MOCK")
        if path.startswith("/v2/positions/"):
            response=requests.Response();response.status_code=404
            raise requests.HTTPError("404",response=response)
        return {"buying_power":"10000"}
    monkeypatch.setattr(buyer,"broker_get",get)
    monkeypatch.setenv("DRY_RUN","true")
    result=buyer.run_once(s=object(),service=object(),now=DAY,
                          path=str(tmp_path/"ledger.sqlite3"))
    assert result["orders"]==10


def test_seller_is_not_changed_by_marketwide_buyer():
    # Code-level invariant: new strategy contains no sell/write exit path.
    import inspect
    source=inspect.getsource(buyer)
    assert 'payload={"symbol":sym,"side":"buy"' in source
    assert source.count('s.post(')==1
    # GET history may ask for past SELL orders to honor the old cooldown.
    # No POST ever contains a sell order payload.
