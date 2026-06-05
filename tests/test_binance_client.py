from __future__ import annotations

from datetime import UTC, datetime
from urllib.error import HTTPError
from io import BytesIO

import pytest

import src.data.binance_client as binance_client
from src.data.binance_client import BinanceSpotKlineClient, Kline, download_spot_klines
from src.exceptions import MarketDataError


class FakeResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _kline_row(open_time_ms: int, open_price: str = "1.0") -> list[object]:
    return [
        open_time_ms,
        open_price,
        "2.0",
        "0.5",
        "1.5",
        "10.0",
        open_time_ms + 3_599_999,
        "15.0",
        100,
        "5.0",
        "7.5",
        "0",
    ]


def test_download_spot_klines_merges_pages_and_filters_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    pages = [
        [_kline_row(1704067200000), _kline_row(1704070800000)],
        [_kline_row(1704074400000), _kline_row(1704078000000)],
    ]

    def fake_fetch(self, *, symbol, interval, start_time, end_time, limit):
        calls.append(
            {
                "symbol": symbol,
                "interval": interval,
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
            }
        )
        rows = pages.pop(0)
        return [binance_client._row_to_kline(symbol, interval, row) for row in rows]

    monkeypatch.setattr(BinanceSpotKlineClient, "fetch_klines_page", fake_fetch)

    result = download_spot_klines(
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 4, 0, tzinfo=UTC),
    )

    assert [k.open_time for k in result] == [
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
    ]
    assert all(k.open_time.tzinfo is UTC for k in result)
    assert len(calls) == 2
    assert calls[0]["start_time"] == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    assert calls[1]["start_time"] == datetime(2024, 1, 1, 2, 0, tzinfo=UTC)


def test_download_spot_klines_returns_empty_for_no_completed_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        BinanceSpotKlineClient,
        "fetch_klines_page",
        lambda *args, **kwargs: [],
    )

    result = download_spot_klines(
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    )

    assert result == []


def test_client_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    responses = [
        HTTPError(
            url="https://api.binance.com/api/v3/klines",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=BytesIO(b'{"code":429,"msg":"Too Many Requests"}'),
        ),
        FakeResponse(
            "["
            "[1704067200000,\"1.0\",\"2.0\",\"0.5\",\"1.5\",\"10.0\",1704070799999,\"15.0\",100,\"5.0\",\"7.5\",\"0\"]"
            "]"
        ),
    ]

    def fake_urlopen(request, timeout):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(binance_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    client = BinanceSpotKlineClient(max_retries=2, retry_delay_seconds=0.1)
    rows = client.fetch_klines_page(
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert sleep_calls == [0.1]


def test_client_retries_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    responses = [
        HTTPError(
            url="https://api.binance.com/api/v3/klines",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=BytesIO(b"server error"),
        ),
        FakeResponse(
            "["
            "[1704067200000,\"1.0\",\"2.0\",\"0.5\",\"1.5\",\"10.0\",1704070799999,\"15.0\",100,\"5.0\",\"7.5\",\"0\"]"
            "]"
        ),
    ]

    def fake_urlopen(request, timeout):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(binance_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance_client.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    client = BinanceSpotKlineClient(max_retries=2, retry_delay_seconds=0.1)
    rows = client.fetch_klines_page(
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
    )

    assert len(rows) == 1
    assert sleep_calls == [0.1]


def test_invalid_symbol_raises_clear_error() -> None:
    with pytest.raises(MarketDataError, match="無效交易對: BTC-USDT"):
        download_spot_klines(
            symbol="BTC-USDT",
            interval="1h",
            start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end_time=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        )


def test_http_400_surfaces_binance_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = HTTPError(
        url="https://api.binance.com/api/v3/klines",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=BytesIO(b'{"code":-1121,"msg":"Invalid symbol."}'),
    )

    monkeypatch.setattr(binance_client, "urlopen", lambda request, timeout: (_ for _ in ()).throw(error))

    client = BinanceSpotKlineClient(max_retries=1)
    with pytest.raises(MarketDataError, match="Binance HTTP 400"):
        client.fetch_klines_page(
            symbol="INVALID",
            interval="1h",
            start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end_time=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        )
