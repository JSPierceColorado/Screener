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
