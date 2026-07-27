"""Setup C 실시간 러너 — 확정 5분봉 스트림 위에서 C1(OI-Flush Reversal Long)과
C2(Coil-Break OI Filter)를 동시에 구동한다. StrategyEngine과는 완전히
독립적이지만, GUI(Dashboard/Settings/PnL/Log)에는 A/B와 동일한 수준으로
연동된다 — 공개 메서드 시그니처는 이전 버전(OU 평균회귀)과 동일하게 유지.

라이브 모드: live_feed.py가 이미 구독 중인 btcusdt@kline_5m 확정봉 +
oi_poll_loop(5분 REST)의 OI 값을 받는다(신규 on_oi 훅).
합성 모드: MinuteAggregator로 1분봉을 5분봉으로 집계해 같은 인터페이스로 공급.
"""

from __future__ import annotations

import time
from collections import deque

import pandas as pd

from .setup_c import (
    ParamsC, compute_features, advance_c1, advance_c2, C1State, C2State,
    conditions_c1, conditions_c2, conditions_c1_live, conditions_c2_live,
)
from .backtest_c import Ledger, _bps, _r_multiple

MAX_BARS_KEPT = 3000   # quantile_lookback_bars(기본 2016) + 여유


class MinuteAggregator:
    """1분봉 -> N초 버킷 집계기. 합성 모드에서 Setup C(5분봉 전용)를 구동하기
    위해서만 쓰인다."""

    def __init__(self, bucket_s: int, on_bar_closed):
        self.bucket_s = bucket_s
        self.on_bar_closed = on_bar_closed
        self._forming = None

    def add_1m(self, ts: float, o: float, h: float, l: float, c: float, v: float):
        bucket_ts = (int(ts) // self.bucket_s) * self.bucket_s
        f = self._forming
        if f is None or f["ts"] != bucket_ts:
            if f is not None:
                self.on_bar_closed(dict(f, closed=True))
            self._forming = {"ts": bucket_ts, "open": o, "high": h, "low": l, "close": c, "volume": v}
        else:
            f["high"] = max(f["high"], h)
            f["low"] = min(f["low"], l)
            f["close"] = c
            f["volume"] += v


class LiveSetupCRunner:
    """확정 5분봉 하나씩 받아 C1/C2를 동시 구동. 실주문 없음(섀도/페이퍼)."""

    def __init__(self, params: ParamsC = None):
        self.p = params or ParamsC()
        self.enabled = True
        self._rows = deque(maxlen=MAX_BARS_KEPT)
        self._latest_oi = None

        self.c1_state = C1State()
        self.c2_state = C2State()
        self.ledger_c1 = Ledger("C1")
        self.ledger_c2 = Ledger("C2")

        self.closed_trades: list[dict] = []
        self.log: list[tuple] = []
        self._c1_conditions: list[dict] = []
        self._c2_conditions: list[dict] = []
        # 마지막 확정봉 기준 게이트/기준값 캐시 — live_conditions()가 매초 체결틱으로
        # IDLE 상태 조건을 다시 계산할 때 rolling quantile을 재정렬하지 않고 이걸 쓴다.
        self._c1_cache: dict = {}
        self._c2_cache: dict = {}

    # ------------------------------------------------------------------
    def on_oi(self, oi_value: float, ts: float):
        """live_engine_bridge.py의 oi_poll(5분 REST) 후크에서 호출."""
        self._latest_oi = oi_value

    def on_confirmed_5m_candle(self, candle: dict):
        """candle: {ts, open, high, low, close, volume, closed(무시)}."""
        row = dict(candle)
        row["oi"] = self._latest_oi if self._latest_oi is not None else float("nan")
        self._rows.append(row)
        if len(self._rows) < 3:
            return  # rolling()/iloc[i-1] 접근이 안전하려면 최소 몇 행은 필요.
                    # 히스토리 부족(quantile_lookback_bars 미만)은 advance_c1/advance_c2
                    # 내부의 _min_history_c1/_min_history_c2 가드가 이미 처리한다.
        f = compute_features(self._to_df(), self.p)
        i = len(f) - 1

        self._step_c1(f, i)
        self._step_c2(f, i)

        self._c1_conditions = conditions_c1(f, i, self.c1_state, self.p)
        self._c2_conditions = conditions_c2(f, i, self.c2_state, self.p)
        row = f.iloc[i]
        self._c1_cache = {
            "atr": row["atr"], "oi_chg_c1_qlo": row["oi_chg_c1_qlo"],
            "rvol_qhi": row["rvol_qhi"], "rvol": row["rvol"],
            "last_conditions": self._c1_conditions,
        }
        self._c2_cache = {
            "realized_range_c2_qlo": row["realized_range_c2_qlo"],
            "oi_trend_c2_qhi": row["oi_trend_c2_qhi"],
            "vol_med_c2": row["vol_med_c2"], "vol_med_c2_qlo": row["vol_med_c2_qlo"],
            "last_conditions": self._c2_conditions,
        }

        if not self.enabled:
            return

        self.c1_state, sig1 = advance_c1(f, i, self.c1_state, self.p)
        if sig1 is not None and self.ledger_c1.can_enter(i):
            self.ledger_c1.open("long", sig1, i)
            self.log.append((sig1.ts.timestamp(),
                              f"Setup C1 LONG 진입 신호 @ {sig1.entry:,.1f} (캐스케이드저점 {sig1.cascade_low:,.1f})"))

        self.c2_state, sig2 = advance_c2(f, i, self.c2_state, self.p)
        if sig2 is not None and self.ledger_c2.can_enter(i):
            self.ledger_c2.open(sig2.side, sig2, i, extra={"kind": sig2.kind})
            self.log.append((sig2.ts.timestamp(),
                              f"Setup C2 {sig2.side.upper()} 진입 신호 @ {sig2.entry:,.1f} ({sig2.kind})"))

    def _step_c1(self, f, i):
        row = f.iloc[i]
        trade = self.ledger_c1.step(i, row["high"], row["low"], row["close"], f.index[i], self.p.c1_max_reentries)
        if trade is not None:
            self._record_closed("C1", trade)

    def _step_c2(self, f, i):
        row = f.iloc[i]
        trade = self.ledger_c2.step(i, row["high"], row["low"], row["close"], f.index[i], self.p.c2_max_reentries)
        if trade is not None:
            self._record_closed("C2", trade)

    def _record_closed(self, setup: str, trade: dict):
        rec = {
            "setup": setup, "side": trade["side"].upper(), "entry_ts": trade["entry_ts"].timestamp(),
            "entry_price": trade["entry_price"], "exit_ts": trade["exit_ts"].timestamp(),
            "exit_price": trade["exit_price"], "reason": trade["reason"],
            "bars_held": trade["bars_held"], "bps": trade["bps"], "r_multiple": trade["r_multiple"],
            "tag": f"{setup}-{trade['side']}",
        }
        self.closed_trades.append(rec)
        self.log.append((rec["exit_ts"],
                          f"Setup {setup} {rec['side']} {rec['reason']} 청산 @ {rec['exit_price']:,.1f} ({rec['bps']:+.1f}bps)"))

    def _to_df(self) -> pd.DataFrame:
        df = pd.DataFrame(list(self._rows))
        df.index = pd.to_datetime(df["ts"], unit="s")
        return df

    # ------------------------------------------------------------------
    # GUI/Settings 연동용 — 공개 인터페이스는 이전 버전과 동일하게 유지
    # ------------------------------------------------------------------
    def stop(self):
        self.ledger_c1.pos = None
        self.ledger_c2.pos = None
        self.enabled = False

    def restart(self, params: ParamsC):
        self.p = params
        self.c1_state = C1State()
        self.c2_state = C2State()
        self.ledger_c1 = Ledger("C1")
        self.ledger_c2 = Ledger("C2")
        self.enabled = True

    def manual_close_all(self, price: float = None, ts: float = None):
        """텔레그램 /close all — 보유 중인 C1/C2 포지션을 현재가로 강제 청산
        기록한다(가상 체결, 실주문 없음 — Setup A/B의 manual_close_all과 동일
        원칙: price가 없으면(아직 체결틱 미수신) 포지션 자신의 진입가로
        대체한다). 정상 스트림 처리(_record_closed)는 pandas Timestamp(캔들
        인덱스)를 받아 .timestamp()를 호출하는데, 여기서는 이미 epoch float인
        time.time()을 exit_ts로 쓰므로 별도로 최종 dict를 직접 만든다."""
        ts = ts if ts is not None else time.time()
        for setup, ledger in (("C1", self.ledger_c1), ("C2", self.ledger_c2)):
            pos = ledger.pos
            if pos is None:
                continue
            exit_price = price if price is not None else pos["entry"]
            bps = _bps(pos["entry"], exit_price, pos["side"])
            r_mult = _r_multiple(pos["entry"], pos["stop"], exit_price, pos["side"])
            rec = {
                "setup": setup, "side": pos["side"].upper(),
                "entry_ts": pos["entry_ts"].timestamp(), "entry_price": pos["entry"],
                "exit_ts": ts, "exit_price": exit_price, "reason": "MANUAL",
                "bars_held": pos["bars_held"], "bps": bps, "r_multiple": r_mult,
                "tag": f"{setup}-{pos['side']}",
            }
            self.closed_trades.append(rec)
            self.log.append((ts, f"Setup {setup} {rec['side']} MANUAL 청산 @ {exit_price:,.1f} ({bps:+.1f}bps)"))
            ledger.pos = None

    def open_legs_view(self) -> list[dict]:
        out = []
        for setup, ledger in (("C1", self.ledger_c1), ("C2", self.ledger_c2)):
            pos = ledger.pos
            if pos is None:
                continue
            out.append({
                "setup": setup, "side": pos["side"].upper(), "entry_price": pos["entry"],
                "qty_fraction": 1.0 if not pos["tp1_hit"] else 1 - pos["tp1_fraction"],
                "sl": pos["stop"], "tp1": pos["tp1"], "tp2": pos["tp2"],
                "tp1_hit": pos["tp1_hit"], "tag": f"{setup}-{pos['side']}",
            })
        return out

    def describe(self) -> str:
        if not self._c1_conditions and not self._c2_conditions:
            return "히스토리 축적 중 (5분봉+OI 데이터 대기)"
        if not self.enabled:
            return "중지됨 — \"적용\"으로 재시작 대기"
        parts = []
        if self._c1_conditions:
            met = sum(1 for c in self._c1_conditions if c["met"])
            parts.append(f"C1 {met}/{len(self._c1_conditions)}")
        if self._c2_conditions:
            met = sum(1 for c in self._c2_conditions if c["met"])
            parts.append(f"C2 {met}/{len(self._c2_conditions)}")
        return " · ".join(parts)

    def summary(self) -> dict:
        out = {}
        for setup in ("C1", "C2"):
            trades = [t for t in self.closed_trades if t["setup"] == setup]
            if not trades:
                out[setup] = {"count": 0}
                continue
            wins = [t for t in trades if t["bps"] > 0]
            out[setup] = {
                "count": len(trades),
                "win_rate": len(wins) / len(trades),
                "avg_bps": sum(t["bps"] for t in trades) / len(trades),
                "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
            }
        return out

    def combined_summary(self) -> dict:
        """C1+C2 합산 — PnL 위젯의 단일 "C" 행에 표시(기존 GUI 스키마 불변)."""
        trades = self.closed_trades
        if not trades:
            return {"count": 0}
        wins = [t for t in trades if t["bps"] > 0]
        return {
            "count": len(trades),
            "win_rate": len(wins) / len(trades),
            "avg_bps": sum(t["bps"] for t in trades) / len(trades),
            "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
            "max_consec_losses": _max_consec_losses(trades),
        }

    @property
    def last_conditions(self) -> list[dict]:
        def _prefix(conds, tag):
            return [{**c, "label": f"{tag} {c['label']}"} for c in conds]
        return _prefix(self._c1_conditions, "[C1]") + _prefix(self._c2_conditions, "[C2]")

    def live_conditions(self, live_price: float = None, live_oi: float = None) -> list[dict]:
        """1초 폴링용 실시간 버전 — IDLE 상태 조건(가격/OI 관련)은 체결 틱으로
        매초 갱신되고, 그 외 상태(봉 카운트 기반 확인 중)는 마지막 확정봉
        기준값을 그대로 보여준다. live_price/live_oi가 아직 없으면(연결 직후)
        last_conditions와 동일하게 동작한다."""
        if live_price is None or not self._rows:
            return self.last_conditions

        def _prefix(conds, tag):
            return [{**c, "label": f"{tag} {c['label']}"} for c in conds]

        c1 = conditions_c1_live(list(self._rows), self.c1_state, self._c1_cache, live_price, live_oi, self.p)
        c2 = conditions_c2_live(list(self._rows), self.c2_state, self._c2_cache, live_price, live_oi, self.p)
        return _prefix(c1, "[C1]") + _prefix(c2, "[C2]")


def _max_consec_losses(trades: list) -> int:
    m = cur = 0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        if t["bps"] <= 0:
            cur += 1
            m = max(m, cur)
        else:
            cur = 0
    return m
