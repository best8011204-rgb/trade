"""Binance USDT-M 선물 서명(Signed) REST 트레이더 — 실제 주문 실행.

`binance_client.py`(공개 데이터 전용)와 달리 여기는 API 키/시크릿으로
HMAC-SHA256 서명을 붙여 실계좌 주문을 낸다. run_bot.py 가 live_trade 모드일
때만 사용하며, 시장가(MARKET) 진입/청산과 reduceOnly 부분 청산만 지원한다
(전략 실행 구조가 시장가 즉시 체결을 전제로 하기 때문).

주의:
- 원웨이(one-way) 포지션 모드를 전제로 한다. 헤지 모드 계좌면 시작 시
  경고를 내고 원웨이로 전환을 시도한다.
- testnet=True 면 https://testnet.binancefuture.com 을 사용한다. 실계좌에
  붙이기 전에 반드시 테스트넷으로 먼저 검증할 것.
"""

import hashlib
import hmac
import math
import sys
import time
from urllib.parse import urlencode

import requests

MAINNET_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"


class TraderError(Exception):
    pass


class BinanceFuturesTrader:
    def __init__(self, api_key: str, api_secret: str, symbol: str = "BTCUSDT",
                 testnet: bool = False, leverage: int = 3, recv_window: int = 5000):
        if not api_key or not api_secret:
            raise TraderError("API 키/시크릿이 비어 있음")
        self.api_key = api_key
        self.api_secret = api_secret.encode()
        self.symbol = symbol.upper()
        self.base_url = TESTNET_URL if testnet else MAINNET_URL
        self.testnet = testnet
        self.leverage = leverage
        self.recv_window = recv_window
        self._session = requests.Session()
        self._session.headers["X-MBX-APIKEY"] = api_key
        self._time_offset_ms = 0

        self._qty_step = None
        self._min_qty = None
        self._min_notional = None

    # ---- 저수준 요청 ---------------------------------------------------
    def _sync_time(self):
        r = self._session.get(self.base_url + "/fapi/v1/time", timeout=10)
        r.raise_for_status()
        self._time_offset_ms = r.json()["serverTime"] - int(time.time() * 1000)

    def _signed(self, method: str, path: str, params: dict = None, retry: bool = True):
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
        params["recvWindow"] = self.recv_window
        query = urlencode(params)
        sig = hmac.new(self.api_secret, query.encode(), hashlib.sha256).hexdigest()
        url = f"{self.base_url}{path}?{query}&signature={sig}"
        r = self._session.request(method, url, timeout=15)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = {"msg": r.text}
            # -1021: 타임스탬프 어긋남 -> 서버시간 재동기화 후 1회 재시도
            if retry and body.get("code") == -1021:
                self._sync_time()
                return self._signed(method, path, {k: v for k, v in params.items()
                                                   if k not in ("timestamp", "recvWindow")}, retry=False)
            raise TraderError(f"{method} {path} 실패 [{r.status_code}]: {body.get('msg', body)}")
        return r.json()

    def _public(self, path: str, params: dict = None):
        r = self._session.get(self.base_url + path, params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json()

    # ---- 초기화 ---------------------------------------------------------
    def prepare(self):
        """서버시간 동기화 + 심볼 필터 로딩 + 레버리지/포지션모드 설정."""
        self._sync_time()
        info = self._public("/fapi/v1/exchangeInfo")
        sym = next((s for s in info["symbols"] if s["symbol"] == self.symbol), None)
        if sym is None:
            raise TraderError(f"심볼 {self.symbol} 을 exchangeInfo에서 찾을 수 없음")
        for f in sym["filters"]:
            if f["filterType"] == "LOT_SIZE":
                self._qty_step = float(f["stepSize"])
                self._min_qty = float(f["minQty"])
            elif f["filterType"] == "MIN_NOTIONAL":
                self._min_notional = float(f.get("notional", 0))

        # 원웨이 모드 확인 (dualSidePosition=True 면 헤지 모드)
        mode = self._signed("GET", "/fapi/v1/positionSide/dual")
        if mode.get("dualSidePosition"):
            print("[trader] 헤지 모드 감지 -> 원웨이 모드로 전환 시도", file=sys.stderr)
            self._signed("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})

        self._signed("POST", "/fapi/v1/leverage", {"symbol": self.symbol, "leverage": self.leverage})

    # ---- 조회 ----------------------------------------------------------
    def get_usdt_balance(self) -> float:
        for b in self._signed("GET", "/fapi/v2/balance"):
            if b["asset"] == "USDT":
                return float(b["availableBalance"])
        return 0.0

    def get_position(self) -> dict:
        """현재 심볼 순포지션. {'qty': 부호있는 수량, 'entry_price', 'unrealized'}"""
        rows = self._signed("GET", "/fapi/v2/positionRisk", {"symbol": self.symbol})
        for row in rows:
            qty = float(row["positionAmt"])
            if row["symbol"] == self.symbol:
                return {
                    "qty": qty,
                    "entry_price": float(row["entryPrice"]),
                    "unrealized": float(row["unRealizedProfit"]),
                    "leverage": row.get("leverage"),
                }
        return {"qty": 0.0, "entry_price": 0.0, "unrealized": 0.0, "leverage": None}

    # ---- 수량 라운딩 ----------------------------------------------------
    def round_qty(self, qty: float) -> float:
        if self._qty_step is None:
            raise TraderError("prepare() 를 먼저 호출해야 함 (LOT_SIZE 미로딩)")
        steps = math.floor(qty / self._qty_step + 1e-9)
        rounded = steps * self._qty_step
        # 부동소수 잔재 제거
        decimals = max(0, int(round(-math.log10(self._qty_step))))
        rounded = round(rounded, decimals)
        if rounded < (self._min_qty or 0):
            return 0.0
        return rounded

    def min_valid_qty(self, price: float) -> float:
        """LOT_SIZE 최소수량과 MIN_NOTIONAL을 동시에 만족하는 최소 주문수량."""
        q = self._min_qty or 0.0
        if self._min_notional and price > 0:
            q = max(q, self._min_notional / price)
        step = self._qty_step or 0.001
        return math.ceil(q / step) * step

    # ---- 주문 ----------------------------------------------------------
    def market_order(self, side: str, qty: float, reduce_only: bool = False) -> dict:
        """시장가 주문. side: 'BUY' | 'SELL'. 반환: 주문 응답(avgPrice 포함될 수 있음)."""
        if qty <= 0:
            raise TraderError(f"주문 수량이 0 이하: {qty}")
        params = {
            "symbol": self.symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f"{qty:.10f}".rstrip("0").rstrip("."),
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._signed("POST", "/fapi/v1/order", params)

    def close_position(self) -> dict | None:
        """심볼의 순포지션 전량 시장가 청산."""
        pos = self.get_position()
        qty = pos["qty"]
        if abs(qty) < (self._min_qty or 1e-12):
            return None
        side = "SELL" if qty > 0 else "BUY"
        return self.market_order(side, abs(qty), reduce_only=True)
