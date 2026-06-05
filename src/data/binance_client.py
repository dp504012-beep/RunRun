from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from json import JSONDecodeError
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import logging
import time

from src.exceptions import MarketDataError

logger = logging.getLogger(__name__)

BINANCE_SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_LIMIT = 1000

INTERVAL_TO_MS: dict[str, int] = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}


@dataclass(slots=True)
class Kline:
    symbol: str
    interval: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class BinanceSpotKlineClient:
    def __init__(
        self,
        base_url: str = BINANCE_SPOT_KLINES_URL,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds

    def fetch_klines_page(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = MAX_LIMIT,
    ) -> list[Kline]:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": _to_utc_millis(start_time),
            "endTime": _to_utc_millis(end_time),
            "limit": min(limit, MAX_LIMIT),
            "timeZone": "0",
        }
        raw_rows = self._get_json(params)
        return [_row_to_kline(symbol, interval, row) for row in raw_rows]

    def _get_json(self, params: dict[str, Any]) -> list[list[Any]]:
        url = f"{self._base_url}?{urlencode(params)}"
        request = Request(url, method="GET")

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    payload = response.read().decode("utf-8")
                data = json.loads(payload)
                if not isinstance(data, list):
                    raise MarketDataError("Binance 回應格式不正確。")
                return data
            except HTTPError as exc:
                last_error = exc
                if exc.code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                    _sleep_backoff(self._retry_delay_seconds, attempt)
                    continue
                raise _http_error_to_market_data_error(exc) from exc
            except (URLError, TimeoutError, JSONDecodeError, OSError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, URLError) and attempt < self._max_retries:
                    _sleep_backoff(self._retry_delay_seconds, attempt)
                    continue
                if isinstance(exc, TimeoutError) and attempt < self._max_retries:
                    _sleep_backoff(self._retry_delay_seconds, attempt)
                    continue
                if attempt < self._max_retries and _is_retryable_transport_error(exc):
                    _sleep_backoff(self._retry_delay_seconds, attempt)
                    continue
                raise MarketDataError(f"無法取得 Binance K 線資料: {exc}") from exc

        raise MarketDataError("無法取得 Binance K 線資料。") from last_error


def download_spot_klines(
    *,
    symbol: str,
    interval: str,
    start_time: datetime,
    end_time: datetime,
    client: BinanceSpotKlineClient | None = None,
) -> list[Kline]:
    client = client or BinanceSpotKlineClient()
    _validate_symbol(symbol)
    interval_ms = _interval_to_ms(interval)

    start_utc = _ensure_utc(start_time)
    end_utc = _ensure_utc(end_time)
    if start_utc >= end_utc:
        raise MarketDataError("start_time 必須早於 end_time。")

    last_complete_open = end_utc - timedelta(milliseconds=interval_ms)
    if start_utc > last_complete_open:
        return []

    results: list[Kline] = []
    seen_open_times: set[datetime] = set()
    cursor = start_utc
    while cursor <= last_complete_open:
        page = client.fetch_klines_page(
            symbol=symbol,
            interval=interval,
            start_time=cursor,
            end_time=end_utc,
            limit=MAX_LIMIT,
        )
        completed_page = [k for k in page if _is_completed(k.open_time, interval_ms, end_utc)]
        if not completed_page:
            break

        for kline in completed_page:
            if kline.open_time >= cursor and kline.open_time not in seen_open_times:
                results.append(kline)
                seen_open_times.add(kline.open_time)

        cursor = completed_page[-1].open_time + timedelta(milliseconds=interval_ms)

    return sorted(results, key=lambda item: item.open_time)


def _row_to_kline(symbol: str, interval: str, row: list[Any]) -> Kline:
    if len(row) < 6:
        raise MarketDataError("Binance K 線資料欄位不足。")
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
        open=Decimal(str(row[1])),
        high=Decimal(str(row[2])),
        low=Decimal(str(row[3])),
        close=Decimal(str(row[4])),
        volume=Decimal(str(row[5])),
    )


def _is_completed(open_time: datetime, interval_ms: int, end_time: datetime) -> bool:
    return open_time + timedelta(milliseconds=interval_ms) <= end_time


def _interval_to_ms(interval: str) -> int:
    try:
        return INTERVAL_TO_MS[interval]
    except KeyError as exc:
        raise MarketDataError(f"不支援的 interval: {interval}") from exc


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_utc_millis(value: datetime) -> int:
    return int(_ensure_utc(value).timestamp() * 1000)


def _validate_symbol(symbol: str) -> None:
    if not symbol or not symbol.isalnum():
        raise MarketDataError(f"無效交易對: {symbol}")


def _is_retryable_transport_error(exc: Exception) -> bool:
    return isinstance(exc, (URLError, TimeoutError))


def _sleep_backoff(base_delay: float, attempt: int) -> None:
    time.sleep(base_delay * (2**attempt))


def _http_error_to_market_data_error(exc: HTTPError) -> MarketDataError:
    try:
        body = exc.read().decode("utf-8")
    except Exception:  # noqa: BLE001
        body = exc.reason if isinstance(exc.reason, str) else "HTTP error"
    return MarketDataError(f"Binance HTTP {exc.code}: {body}")
