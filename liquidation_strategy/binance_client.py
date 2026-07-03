"""Binance USDT-M 선물 공개 REST 엔드포인트 래퍼.

인증이 필요 없는 공개 마켓 데이터만 사용한다 (API 키 불필요).
이 세션의 샌드박스는 fapi.binance.com 아웃바운드가 조직 정책으로 막혀 있어
여기서는 직접 실행할 수 없다 — 인터넷이 열린 환경(로컬 머신 등)에서 실행할 것.

주의(명세서 4장): forceOrder(강제청산) 스트림은 공개 과거 이력 REST가 없다
(라이브 웹소켓만 존재). 따라서 Setup A는 REST 백필이 불가능하고, live_feed.py로
스트림을 직접 녹화해 나가야 한다. Setup B는 klines + openInterestHist만으로
과거 데이터 백테스트가 가능하다.
"""

import time
import requests

BASE_URL = "https://fapi.binance.com"
_SESSION = requests.Session()


def _get(path, params=None, timeout=15):
    r = _SESSION.get(BASE_URL + path, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_klines(symbol="BTCUSDT", interval="1m", start_ms=None, end_ms=None, limit=1500):
    """GET /fapi/v1/klines — 공개, 인증 불필요. 최대 1500개/요청."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    return _get("/fapi/v1/klines", params)


def get_klines_range(symbol, interval, start_ms, end_ms, pause_s=0.25):
    """페이지네이션으로 [start_ms, end_ms) 구간 전체 클라인 수집."""
    interval_ms = _interval_to_ms(interval)
    out = []
    cursor = start_ms
    while cursor < end_ms:
        batch = get_klines(symbol, interval, start_ms=cursor, end_ms=end_ms, limit=1500)
        if not batch:
            break
        out.extend(batch)
        last_open = batch[-1][0]
        cursor = last_open + interval_ms
        if len(batch) < 1500:
            break
        time.sleep(pause_s)  # 레이트리밋 여유
    return out


def get_open_interest(symbol="BTCUSDT"):
    """GET /fapi/v1/openInterest — 현재 시점 OI (공개)."""
    return _get("/fapi/v1/openInterest", {"symbol": symbol})


def get_open_interest_hist(symbol="BTCUSDT", period="5m", start_ms=None, end_ms=None, limit=500):
    """GET /futures/data/openInterestHist — 과거 OI (공개, 최근 최대 30일까지만 제공)."""
    params = {"symbol": symbol, "period": period, "limit": limit}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    return _get("/futures/data/openInterestHist", params)


def get_open_interest_hist_range(symbol, period, start_ms, end_ms, pause_s=0.3):
    period_ms = _interval_to_ms(period)
    out = []
    cursor = start_ms
    while cursor < end_ms:
        batch = get_open_interest_hist(symbol, period, start_ms=cursor, end_ms=end_ms, limit=500)
        if not batch:
            break
        out.extend(batch)
        last_ts = batch[-1]["timestamp"]
        cursor = last_ts + period_ms
        if len(batch) < 500:
            break
        time.sleep(pause_s)
    return out


def get_agg_trades(symbol="BTCUSDT", start_ms=None, end_ms=None, from_id=None, limit=1000):
    """GET /fapi/v1/aggTrades — 과거 체결(CVD 재구성용), 공개."""
    params = {"symbol": symbol, "limit": limit}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    if from_id is not None:
        params["fromId"] = from_id
    return _get("/fapi/v1/aggTrades", params)


def _interval_to_ms(s):
    unit = s[-1]
    n = int(s[:-1])
    mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
    return n * mult
