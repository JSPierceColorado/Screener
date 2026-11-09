from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import main


def test_parse_constituents_accepts_component_table():
    html = "<table><tr><th>Symbol</th><th>Name</th><th>Sector</th></tr>" + "".join(
        f"<tr><td>{ticker}</td><td>Company</td><td>Sector</td></tr>"
        for ticker in [f"SYM{i}" for i in range(100)] + ["BRK.B"]
    ) + "</table>"
    result = main.parse_constituents(html)
    assert len(result) == 101
    assert "BRK.B" in result


def test_rejects_incomplete_or_changed_components_page():
    with pytest.raises(ValueError):
        main.parse_constituents("<html><h1>Components</h1></html>")


def test_atomic_update_limits_both_operations_to_a_to_f():
    commands = main.build_atomic_update(0, 13042, [["AAPL", 1, 2, 3, 0.5, 4]] * 101)
    assert len(commands) == 2
    assert commands[0]["updateCells"]["range"]["endColumnIndex"] == 6
    assert commands[0]["updateCells"]["range"]["endRowIndex"] == 13042
    assert commands[1]["updateCells"]["start"]["columnIndex"] == 0
    assert all(len(r["values"]) == 6 for r in commands[1]["updateCells"]["rows"])


def test_calculate_252_bars_and_indicators(monkeypatch):
    monkeypatch.delenv("MIN_VALID_SYMBOLS", raising=False)
    bars = [{"t": "2026-01-01T05:00:00Z", "c": 10., "h": 15., "l": 5., "v": 1000000} for _ in range(252)]
    rows, missing = main.calculate_rows(["AAPL"], {"AAPL": bars})
    assert rows == [["AAPL", 10, 10, 10, 0.5, 10]]
    assert missing == []


def test_prevalidation_keeps_data_if_insufficient_bars(monkeypatch):
    monkeypatch.setenv("MIN_VALID_SYMBOLS", "2")
    with pytest.raises(RuntimeError, match="refusing to erase"):
        main.calculate_rows(["AAPL", "MSFT"], {"AAPL": [{"c": 10, "h": 12, "l": 8, "v": 100}] * 252, "MSFT": []})


def test_closed_weekend_does_not_read_list_or_write(monkeypatch):
    def wrong(*args, **kwargs):
        raise AssertionError("Should not be called on weekend")
    monkeypatch.setattr(main, "get_sp100_symbols", wrong)
    assert main.refresh(session=Mock(), now=datetime(2026, 10, 18, 9, 0, tzinfo=main.DENVER)) is False


def test_header_verification_prevents_write():
    service = Mock()
    methods = service.spreadsheets.return_value
    methods.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Screener", "sheetId": 0, "gridProperties": {"rowCount": 13042}}}]}
    methods.values.return_value.get.return_value.execute.return_value = {"values": [["unexpected"]]}
    with pytest.raises(RuntimeError, match="headers"):
        main.update_screener(service, [["AAPL", 1, 2, 3, .5, 4]])
    methods.batchUpdate.assert_not_called()


def test_fetch_bars_ignores_today_even_if_alpaca_returns_partial(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "example")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "example")
    sess = Mock()
    old = {"t": "2026-10-16T04:00:00Z", "c": 1, "h": 1, "l": 1, "v": 123}
    today = {"t": "2026-10-19T04:00:00Z", "c": 2, "h": 2, "l": 2, "v": 50}
    resp = Mock()
    resp.json.return_value = {"bars": {"AAPL": [old, today]}, "next_page_token": None}
    sess.get.return_value = resp
    result = main.get_daily_bars(sess, ["AAPL"], date(2026, 10, 19))
    assert result["AAPL"] == [old]


def test_weekday_exchange_holiday_skips_without_writing(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "example")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "example")
    reply = Mock()
    reply.json.return_value = []  # Alpaca has no session for this day
    session = Mock()
    session.get.return_value = reply
    monkeypatch.setattr(main, "get_sp100_symbols", lambda *_: pytest.fail("Unexpected list read"))
    result = main.refresh(session=session, now=datetime(2026, 12, 25, 9, 0, tzinfo=main.DENVER))
    assert result is False


def test_successful_google_batch_write_only_targets_screener_a_f():
    service = Mock()
    sheets = service.spreadsheets.return_value
    sheets.get.return_value.execute.return_value = {
        "sheets": [
            {"properties": {"title": "Screener", "sheetId": 0, "gridProperties": {"rowCount": 13042}}},
            {"properties": {"title": "SellerState", "sheetId": 1783450356, "gridProperties": {"rowCount": 171}}},
        ]}
    sheets.values.return_value.get.return_value.execute.return_value = {"values": [main.EXPECTED_HEADERS]}
    main.update_screener(service, [["AAPL", 200, 180, 190, .5, 210]])
    kwargs = sheets.batchUpdate.call_args.kwargs
    assert kwargs["spreadsheetId"] == main.SPREADSHEET_ID
    clear_op, write_op = kwargs["body"]["requests"]
    assert clear_op["updateCells"]["range"] == {
        "sheetId": 0, "startRowIndex": 1, "endRowIndex": 13042,
        "startColumnIndex": 0, "endColumnIndex": 6}
    assert write_op["updateCells"]["start"] == {"sheetId": 0, "rowIndex": 1, "columnIndex": 0}
    assert len(write_op["updateCells"]["rows"][0]["values"]) == 6
    sheets.batchUpdate.return_value.execute.assert_called_once()


def test_open_session_and_market_clock(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "example")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "example")
    calendar_resp = Mock()
    calendar_resp.json.return_value = [{"date": "2026-10-19", "open": "09:30", "close": "16:00"}]
    clock_resp = Mock()
    clock_resp.json.return_value = {"is_open": True}
    session = Mock()
    session.get.side_effect = [calendar_resp, clock_resp]
    assert main.is_market_day(session, date(2026, 10, 19)) is True
    assert session.get.call_count == 2


def test_original_daily_9am_mountain_window_handles_dst():
    utc = timezone.utc
    # Mountain Daylight Time: 15:00 UTC = 09:00 MDT.
    assert main.within_original_daily_scan_window(
        datetime(2026, 7, 6, 15, 0, tzinfo=utc))
    assert main.within_original_daily_scan_window(
        datetime(2026, 7, 6, 15, 40, tzinfo=utc))
    assert not main.within_original_daily_scan_window(
        datetime(2026, 7, 6, 16, 0, tzinfo=utc))
    # Mountain Standard Time: 16:00 UTC = 09:00 MST.
    assert not main.within_original_daily_scan_window(
        datetime(2026, 11, 9, 15, 0, tzinfo=utc))
    assert main.within_original_daily_scan_window(
        datetime(2026, 11, 9, 16, 0, tzinfo=utc))
    assert not main.within_original_daily_scan_window(
        datetime(2026, 11, 9, 17, 0, tzinfo=utc))
    # Weekends never refresh.
    assert not main.within_original_daily_scan_window(
        datetime(2026, 11, 8, 16, 0, tzinfo=utc))


def test_early_or_late_utc_cron_invocation_skips_without_api_or_sheet(monkeypatch):
    def dangerous(*args, **kwargs):
        raise AssertionError("Skipped cron invocation must not call Alpaca, Wikipedia or Sheets")
    monkeypatch.setattr(main,"is_market_day",dangerous)
    monkeypatch.setattr(main,"get_sp100_symbols",dangerous)
    monkeypatch.setattr(main,"update_screener",dangerous)
    # Summer second cron at 10am Denver, winter first at 8am Denver.
    assert main.refresh(session=Mock(),
        now=datetime(2026,7,6,16,0,tzinfo=timezone.utc)) is False
    assert main.refresh(session=Mock(),
        now=datetime(2026,11,9,15,0,tzinfo=timezone.utc)) is False


def test_daily_guard_allows_the_original_9am_mountain_before_alpaca_checks(monkeypatch):
    sentinel=[]
    def mock_market_day(session, date):
        sentinel.append(date.isoformat())
        return False
    monkeypatch.setattr(main,"is_market_day",mock_market_day)
    monkeypatch.setattr(main,"get_sp100_symbols",
                        lambda *_: pytest.fail("Calendar says closed; must not read symbols"))
    assert main.refresh(session=Mock(),
        now=datetime(2026,11,9,16,0,tzinfo=timezone.utc)) is False
    assert sentinel==["2026-11-09"]


def test_one_shot_entrypoint_exits_and_is_not_persistent_worker(monkeypatch):
    monkeypatch.setattr(main,"refresh",lambda **kwargs:False)
    monkeypatch.setattr("sys.argv",["main.py","--once"])
    main.main()  # returns; does not enter the old APScheduler loop


def test_persistent_scheduler_uses_original_timezone_and_hour(monkeypatch):
    import sys
    import types
    scheduled={}
    class StubScheduler:
        def __init__(self, **kwargs):
            scheduled["init"]=kwargs
        def add_job(self, callback, kind, **kwargs):
            scheduled["job"]=(callback,kind,kwargs)
        def start(self):
            scheduled["started"]=True
    import apscheduler.schedulers.blocking as blocking
    monkeypatch.setattr(blocking,"BlockingScheduler",StubScheduler)
    monkeypatch.setattr(sys,"argv",["main.py"])
    main.main()
    assert scheduled["started"] is True
    assert scheduled["init"]["timezone"]==main.DENVER
    callback,kind,options=scheduled["job"]
    assert kind=="cron"
    assert (options["hour"],options["minute"],options["day_of_week"])==(9,0,"mon-fri")
    assert callback==main.scheduled_refresh


def test_manual_force_can_refresh_outside_time_guard(monkeypatch):
    import sys
    observed=[]
    monkeypatch.setattr(main,"refresh",lambda **kwargs:observed.append(kwargs) or True)
    monkeypatch.setattr(sys,"argv",["main.py","--once","--force"])
    main.main()
    assert observed==[{"force":True}]


def test_failed_scheduled_check_does_not_stop_future_scheduler(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("mock public API temporarily unavailable")
    monkeypatch.setattr(main,"refresh",fail)
    main.scheduled_refresh()  # failure is reported but scheduler stays alive


def test_market_session_marker_validates_recent_consensus():
    from datetime import date
    recent=[{"t":"2026-11-06T05:00:00Z"}]*252
    bars={f"SYM{i}":list(recent) for i in range(101)}
    assert main.validated_completed_session(bars,list(bars),date(2026,11,9))=="2026-11-06"


def test_old_or_partial_daily_data_cannot_publish_marker():
    from datetime import date
    stale=[{"t":"2026-10-21T04:00:00Z"}]*252
    bars={f"SYM{i}":list(stale) for i in range(101)}
    with pytest.raises(RuntimeError,match="Stale"):
        main.validated_completed_session(bars,list(bars),date(2026,11,9))
    partial=[{"t":"2026-11-09T05:00:00Z"}]*252
    with pytest.raises(RuntimeError,match="Partial"):
        main.validated_completed_session(
            {f"SYM{i}":list(partial) for i in range(101)},
            [f"SYM{i}" for i in range(101)],date(2026,11,9))


def test_screener_health_stamp_same_atomic_batch_preserves_g():
    service=Mock()
    sheets=service.spreadsheets.return_value
    sheets.get.return_value.execute.return_value={"sheets":[
        {"properties":{"title":"Screener","sheetId":0,
                       "gridProperties":{"rowCount":13042,"columnCount":7}}}
    ]}
    sheets.values.return_value.get.return_value.execute.return_value={
        "values":[main.EXPECTED_HEADERS]}
    main.update_screener(service,[["AAPL",200,180,190,.5,200]],
       scan_utc="2026-11-09T16:05:00+00:00",completed_session="2026-11-06")
    operations=sheets.batchUpdate.call_args.kwargs["body"]["requests"]
    assert len(operations)==4
    assert operations[0]["appendDimension"]=={
        "sheetId":0,"dimension":"COLUMNS","length":3}
    assert operations[1]["updateCells"]["range"]["endColumnIndex"]==6
    assert operations[2]["updateCells"]["start"]["columnIndex"]==0
    stamp=operations[3]["updateCells"]
    assert stamp["start"]["columnIndex"]==8
    assert stamp["rows"][1]["values"][1]["userEnteredValue"]["stringValue"]=="2026-11-06"
