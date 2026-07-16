"""Setup C 실시간 러너 — 확정 5분봉 스트림 위에서 evaluate_bar/should_exit를
한 봉씩 구동한다. StrategyEngine과는 완전히 독립적이지만(엔진 코드 무수정),
GUI(Dashboard/Settings/PnL/Log)에는 A/B와 동일한 수준으로 연동된다.

라이브 모드: live_feed.py가 이미 구독 중인 btcusdt@kline_5m 확정봉을 그대로
받는다 (실데이터, 집계 없음).
합성 모드: MinuteAggregator로 1분봉을 5분봉으로 직접 집계해 같은 인터페이스로
공급한다 (candle_chart.py가 표시용으로 하는 집계와 동일한 방식, 별도 모듈).
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from .setup_c import ParamsC, evaluate_bar, should_exit, conditions as gate_conditions
from .backtest_c import Ledger, _bps, _r_multiple

MAX_BARS_KEPT = 500
COST_BPS = 10.0


def _max_consec_losses(trades: list) -> int:
    m = cur = 0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        if t["bps"] <= 0:
            cur += 1
            m = max(m, cur)
        else:
            cur = 0
    return m


class MinuteAggregator:
    """1분봉 -> N초 버킷 집계기. 합성 모드에서 Setup C(5분봉 전용)를 구동하기
    위해서만 쓰인다 — 라이브 모드는 바이낸스가 5분봉을 직접 주므로 불필요."""

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
    """확정 5분봉 하나씩 받아 Setup C를 구동. 실주문 없음(섀도/페이퍼 전용,
    A/B와 동일한 프로젝트 전역 원칙)."""

    def __init__(self, params: ParamsC = None):
        self.p = params or ParamsC()
        self.enabled = True
        self._rows = deque(maxlen=MAX_BARS_KEPT)
        self.ledgers = {"long": Ledger("long"), "short": Ledger("short")}
        self.closed_trades: list[dict] = []
        self.log: list[tuple] = []          # (ts, msg) — A/B의 log 리스트와 동일 패턴
        self._last_conditions: list[dict] = []

    # ------------------------------------------------------------------
    def on_confirmed_5m_candle(self, candle: dict):
        """candle: {ts, open, high, low, close, volume, closed(무시)}."""
        self._rows.append(candle)
        if len(self._rows) < 2:
            return
        df = self._to_df()
        i = len(df) - 1

        for side, ledger in self.ledgers.items():
            pos = ledger.open_pos
            if pos is None:
                continue
            pos["bars_held"] += 1
            reason = should_exit(df, i, pos["sig"], pos["bars_held"], self.p)
            if reason is None:
                continue
            exit_price = float(pos["sig"].stop) if reason == "sl" else float(df["close"].iloc[i])
            exit_ts = df.index[i].timestamp()
            bps = _bps(pos["sig"], exit_price, side) - COST_BPS
            trade = {
                "setup": "C", "side": side.upper(), "entry_ts": pos["sig"].ts.timestamp(),
                "entry_price": pos["sig"].entry, "exit_ts": exit_ts, "exit_price": exit_price,
                "reason": reason.upper(), "bars_held": pos["bars_held"], "bps": bps,
                "r_multiple": _r_multiple(pos["sig"], exit_price, side), "tag": f"C-{side}",
            }
            self.closed_trades.append(trade)
            self.log.append((exit_ts, f"Setup C {side.upper()} {reason} 청산 @ {exit_price:,.1f} ({bps:+.1f}bps)"))
            ledger._on_exit(reason, i, pos["sig"].half_life_bars, self.p)
            ledger.open_pos = None

        self._last_conditions = gate_conditions(df, i, self.p)

        if not self.enabled:
            return
        res = evaluate_bar(df, i, self.p)
        if not res["pass"]:
            return
        sig = res["signal"]
        ledger = self.ledgers[sig.side]
        if ledger.can_enter(i):
            ledger.open_pos = {"sig": sig, "bars_held": 0}
            self.log.append((sig.ts.timestamp(),
                              f"Setup C {sig.side.upper()} 진입 신호 @ {sig.entry:,.1f} (z={sig.z:+.2f}, VR={sig.vr:.2f})"))

    def _to_df(self) -> pd.DataFrame:
        df = pd.DataFrame(list(self._rows))
        df.index = pd.to_datetime(df["ts"], unit="s")
        return df

    # ------------------------------------------------------------------
    # GUI/Settings 연동용
    # ------------------------------------------------------------------
    def stop(self):
        """진행 중 포지션은 기록 없이 버리고, 재시작 전까지 신규 진입을 멈춘다
        (StrategyEngine.stop_setup과 동일한 의미론)."""
        for ledger in self.ledgers.values():
            ledger.open_pos = None
        self.enabled = False

    def restart(self, params: ParamsC):
        """새 파라미터로 초기 상태부터 즉시 재시작 (StrategyEngine.restart_setup과 동일)."""
        self.p = params
        for ledger in self.ledgers.values():
            ledger.open_pos = None
            ledger.reentry_count = 0
            ledger.cooldown_until_bar = None
        self.enabled = True

    def open_legs_view(self) -> list[dict]:
        """A/B의 OpenLeg 직렬화와 같은 모양으로 맞춰 position 테이블에 나란히 표시.
        C는 TP가 고정가가 아니라 z_exit 조건이라 tp1/tp2는 None(=GUI에 "-")."""
        out = []
        for side, ledger in self.ledgers.items():
            pos = ledger.open_pos
            if pos is None:
                continue
            sig = pos["sig"]
            out.append({
                "setup": "C", "side": side.upper(), "entry_price": sig.entry,
                "qty_fraction": 1.0, "sl": sig.stop, "tp1": None, "tp2": None,
                "tp1_hit": False, "tag": f"C-{side}",
            })
        return out

    def describe(self) -> str:
        """대시보드 상단 요약 1줄 (Setup A/B의 describe()와 동일한 역할)."""
        if not self._last_conditions:
            return "히스토리 축적 중 (5분봉 데이터 대기)"
        met = sum(1 for c in self._last_conditions if c["met"])
        total = len(self._last_conditions)
        if not self.enabled:
            return f"중지됨 (충족조건 {met}/{total}) — \"적용\"으로 재시작 대기"
        if met == total:
            return f"모든 게이트 충족({met}/{total}) — 다음 확정봉에 진입 판정"
        failed = [c["label"] for c in self._last_conditions if not c["met"]]
        return f"게이트 대기 중({met}/{total}) — 미충족: {', '.join(failed)}"

    def summary(self) -> dict:
        out = {}
        for side in ("long", "short"):
            trades = [t for t in self.closed_trades if t["side"] == side.upper()]
            if not trades:
                out[side] = {"count": 0}
                continue
            wins = [t for t in trades if t["bps"] > 0]
            out[side] = {
                "count": len(trades),
                "win_rate": len(wins) / len(trades),
                "avg_bps": sum(t["bps"] for t in trades) / len(trades),
                "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
            }
        return out

    def combined_summary(self) -> dict:
        """롱+숏 합산 — Setup A/B와 동일한 모양(count/win_rate/avg_bps/avg_r/
        max_consec_losses)으로 맞춰 PnL 위젯에 한 행("C")으로 표시하기 위함.
        방향별 상세는 summary()/Log 탭의 개별 트레이드 로그로 확인한다."""
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
        return self._last_conditions
