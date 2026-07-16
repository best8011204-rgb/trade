"""전략 엔진 — Setup A / B 트리거를 합치고 포지션 라이프사이클을 관리한다.

명세서 3장 규칙:
  - B 포지션 보유 중 A 트리거 발생 시: B의 TP2를 청산 소진 시점으로 동적 전환
  - A 롱 진입은 B 청산 완료 후에만 허용 (양방향 동시 보유 금지)
"""

from dataclasses import dataclass, field
from .data_types import Trade, Side, Candle, ForceOrder, OIPoint
from .setup_a import CascadeExhaustionLong, CascadeAParams
from .setup_b import TrappedLongFlushShort, CascadeBParams

ROUNDTRIP_COST_BPS = 10.0  # 명세서 0장: 왕복 비용 8~12bps 가정 (중간값)


@dataclass
class OpenLeg:
    trade: Trade
    sl: float
    tp1: float
    tp2: float
    tp1_hit: bool = False
    time_exit_ts: float = None


class StrategyEngine:
    def __init__(
        self,
        a_params: CascadeAParams = None,
        b_params: CascadeBParams = None,
        macro_blackouts=None,
        cost_bps: float = ROUNDTRIP_COST_BPS,
    ):
        self.a = CascadeExhaustionLong(a_params, macro_blackouts)
        self.b = TrappedLongFlushShort(b_params)
        self.cost_bps = cost_bps

        self.open_legs: list[OpenLeg] = []   # 현재 보유 중인 트랜치들
        self.closed_trades: list[Trade] = []
        self.box_low = None
        self.box_high = None

        self._a_wait_for_b_flat = False

        # GUI/텔레그램의 "중지" 기능용. False면 해당 셋업의 트리거 감지/신규
        # 진입을 전부 건너뛴다 (기존 보유 포지션 관리는 계속된다 — stop_setup
        # 호출 시 해당 셋업 포지션은 이미 버려지므로 실제로는 영향 없음).
        self.a_enabled = True
        self.b_enabled = True

    # ------------------------------------------------------------------
    # 외부에서 매 캔들마다 박스(4h 레인지) 값을 갱신해준다고 가정
    def set_box(self, box_low: float, box_high: float):
        self.box_low, self.box_high = box_low, box_high
        self.b.update_box(box_high)

    def _has_side(self, side: Side) -> bool:
        return any(leg.trade.side == side for leg in self.open_legs)

    def _has_setup(self, setup: str) -> bool:
        return any(leg.trade.setup == setup for leg in self.open_legs)

    def on_force_order(self, fo: ForceOrder):
        if not self.a_enabled:
            return
        was_idle = self.a.state == "IDLE"
        self.a.on_force_order(fo)
        # A 트리거(T1)가 새로 발생 && B 포지션 보유 중이면 B의 TP2를 동적 전환
        if was_idle and self.a.state == "CASCADE" and self._has_setup("B"):
            for leg in self.open_legs:
                if leg.trade.setup == "B":
                    leg.tp2 = fo.price  # 청산 캐스케이드 발생 지점을 TP2로 재설정
                    self.b.log.append((fo.ts, f"A트리거 발생 -> B TP2를 {fo.price:.1f}로 동적 전환"))

    def on_oi(self, pt: OIPoint):
        if self.b_enabled:
            self.b.on_oi(pt)

    def on_candle(self, c: Candle, oi_now: float = None):
        self._manage_open_legs(c)
        if self.a_enabled:
            self.a.on_candle(c)
        if self.box_high is not None and self.b_enabled:
            self.b.on_candle(c, oi_now=oi_now)

        if self.a_enabled:
            self._try_enter_a(c)
        if self.b_enabled:
            self._try_enter_b(c)

    # ------------------------------------------------------------------
    # GUI/텔레그램의 실시간 중지·재시작 (Settings 페이지 "중지"/"적용" 버튼)
    # ------------------------------------------------------------------
    def stop_setup(self, setup: str):
        """해당 셋업의 트리거 감지를 즉시 멈춘다. 진행 중이던 포지션이 있으면
        결과를 기록하지 않고(승패/bps 집계에 남기지 않고) 그냥 버린다.
        restart_setup()을 호출하기 전까지는 새 신호를 전혀 만들지 않는다."""
        self.open_legs = [leg for leg in self.open_legs if leg.trade.setup != setup]
        self._a_wait_for_b_flat = False  # A의 대기 신호가 setup 무관하게 걸려있을 수 있어 항상 정리
        if setup == "A":
            self.a_enabled = False
        else:
            self.b_enabled = False

    def restart_setup(self, setup: str, params):
        """해당 셋업을 새 파라미터로 초기 상태부터 즉시 다시 시작한다.
        stop_setup()과 동일하게 진행 중이던 포지션은 기록 없이 버린다."""
        self.open_legs = [leg for leg in self.open_legs if leg.trade.setup != setup]
        self._a_wait_for_b_flat = False
        if setup == "A":
            self.a = CascadeExhaustionLong(params, self.a.macro_blackouts)
            self.a_enabled = True
        else:
            self.b = TrappedLongFlushShort(params, self.b.box_lookback_s)
            self.b_enabled = True

    # ------------------------------------------------------------------
    def _try_enter_a(self, c: Candle):
        sig = self.a.consume_signal()
        if not sig:
            return
        self.a.reset()  # 탐지 상태를 IDLE로 되돌려 다음 캐스케이드 탐지를 재개
        # 양방향 동시 보유 금지: B 포지션이 열려 있으면 A 진입 보류
        if self._has_setup("B"):
            self._a_wait_for_b_flat = sig
            return
        self._open_a(sig, c)

    def _open_a(self, sig, c):
        p = self.a.p
        sl, tp1, tp2 = CascadeExhaustionLong.compute_exits(
            sig["price"], sig["cascade_low"], sig["cascade_start_price"], p
        )
        trade = Trade(
            setup="A", side=Side.LONG, entry_ts=sig["ts"], entry_price=sig["price"],
            qty_fraction=1.0, tag=sig["tag"],
        )
        self.open_legs.append(OpenLeg(trade, sl, tp1, tp2, time_exit_ts=sig["ts"] + p.time_exit_s))

    def _try_enter_b(self, c: Candle):
        sig1 = self.b.consume_signal()
        if sig1:
            self._open_b_tranche(sig1, c, fraction=self.b.p.tp1_fraction)
        sig2 = self.b.consume_tranche2()
        if sig2:
            self._open_b_tranche(sig2, c, fraction=1 - self.b.p.tp1_fraction)

    def _open_b_tranche(self, sig, c, fraction):
        p = self.b.p
        sl, tp1, tp2 = TrappedLongFlushShort.compute_exits(
            sig["price"], sig["sweep_high"], self.box_high, self.box_low, p
        )
        trade = Trade(
            setup="B", side=Side.SHORT, entry_ts=sig["ts"], entry_price=sig["price"],
            qty_fraction=fraction, tag=sig["tag"],
        )
        self.open_legs.append(OpenLeg(trade, sl, tp1, tp2, time_exit_ts=sig["ts"] + p.time_exit_s))

    # ------------------------------------------------------------------
    def _manage_open_legs(self, c: Candle):
        still_open = []
        for leg in self.open_legs:
            t = leg.trade
            closed = False
            if t.side == Side.LONG:
                if c.low <= leg.sl:
                    closed = self._close(leg, c.ts, leg.sl, "SL", t.qty_fraction)
                elif not leg.tp1_hit and c.high >= leg.tp1:
                    self._close(leg, c.ts, leg.tp1, "TP1", self.a.p.tp1_fraction if t.setup == "A" else 0.5)
                    leg.tp1_hit = True
                    leg.trade = Trade(**{**leg.trade.__dict__, "qty_fraction": 1 - (self.a.p.tp1_fraction if t.setup == "A" else 0.5)})
                    still_open.append(leg)
                    continue
                elif leg.tp1_hit and c.high >= leg.tp2:
                    closed = self._close(leg, c.ts, leg.tp2, "TP2", leg.trade.qty_fraction)
                elif c.ts - t.entry_ts >= (leg.time_exit_ts - t.entry_ts) and c.ts >= leg.time_exit_ts:
                    closed = self._close(leg, c.ts, c.close, "TIME", leg.trade.qty_fraction)
            else:  # SHORT (Setup B)
                if c.high >= leg.sl:
                    closed = self._close(leg, c.ts, leg.sl, "SL", t.qty_fraction)
                elif not leg.tp1_hit and c.low <= leg.tp1:
                    frac_now = leg.trade.qty_fraction * 0.5
                    self._close(leg, c.ts, leg.tp1, "TP1", frac_now)
                    leg.tp1_hit = True
                    leg.trade = Trade(**{**leg.trade.__dict__, "qty_fraction": leg.trade.qty_fraction - frac_now})
                    still_open.append(leg)
                    continue
                elif leg.tp1_hit and c.low <= leg.tp2:
                    closed = self._close(leg, c.ts, leg.tp2, "TP2", leg.trade.qty_fraction)
                elif c.ts >= leg.time_exit_ts:
                    closed = self._close(leg, c.ts, c.close, "TIME", leg.trade.qty_fraction)

            if not closed:
                still_open.append(leg)
        self.open_legs = still_open

        # A 진입 보류 중이었는데 B가 모두 청산됐다면 지금 진입
        if self._a_wait_for_b_flat and not self._has_setup("B"):
            sig = self._a_wait_for_b_flat
            self._a_wait_for_b_flat = False
            self._open_a(sig, c)

    def _close(self, leg: OpenLeg, ts, price, reason, fraction) -> bool:
        t = leg.trade
        risk = abs(t.entry_price - leg.sl)
        pnl = (price - t.entry_price) if t.side == Side.LONG else (t.entry_price - price)
        r_mult = pnl / risk if risk else 0.0
        bps = (pnl / t.entry_price) * 10000 - self.cost_bps

        closed_trade = Trade(
            setup=t.setup, side=t.side, entry_ts=t.entry_ts, entry_price=t.entry_price,
            exit_ts=ts, exit_price=price, qty_fraction=fraction, reason=reason,
            r_multiple=r_mult, bps=bps, tag=t.tag,
        )
        self.closed_trades.append(closed_trade)

        # Setup A 재진입 규칙: 손절 후 동일 캐스케이드에 한해 1회까지 허용
        if t.setup == "A" and reason == "SL" and t.tag == f"A-{self.a.cascade_id}":
            if self.a.allow_reentry():
                self.a.register_reentry()
            else:
                self.a.reset()
        return True

    # ------------------------------------------------------------------
    def summary(self):
        if not self.closed_trades:
            return {}
        out = {}
        for setup in ("A", "B"):
            trades = [t for t in self.closed_trades if t.setup == setup]
            if not trades:
                out[setup] = {"count": 0}
                continue
            wins = [t for t in trades if t.bps > 0]
            out[setup] = {
                "count": len(trades),
                "win_rate": len(wins) / len(trades),
                "avg_bps": sum(t.bps for t in trades) / len(trades),
                "avg_r": sum(t.r_multiple for t in trades) / len(trades),
                "max_consec_losses": _max_consec_losses(trades),
            }
        return out


def _max_consec_losses(trades):
    m = cur = 0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        if t.bps <= 0:
            cur += 1
            m = max(m, cur)
        else:
            cur = 0
    return m
