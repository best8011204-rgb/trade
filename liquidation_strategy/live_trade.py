"""실시간 스트림 -> 엔진 -> 실주문 실행 러너.

LiveShadowRunner(live_feed.py)를 상속해서, 엔진이 만드는 가상 진입/청산을
BinanceFuturesTrader 의 실제 시장가 주문으로 미러링한다.

- trader=None 이면 주문 없이 알림만 내보내는 페이퍼 모드로 동작한다.
- paused=True 면 신규 진입을 실행하지 않는다 (이미 열린 실포지션의
  청산 주문은 계속 나간다 — 방치된 포지션이 생기지 않도록).
- 모든 진입/청산/오류는 notify 콜백(텔레그램)으로 전송된다.

수량 산정: 포지션 1개(태그)당 명목가 trade_usdt 를 진입가로 나눠 계산하고,
트랜치(qty_fraction)에 비례해 배분한다. 거래소 LOT_SIZE/MIN_NOTIONAL 필터를
만족하도록 라운딩하며, 최소 주문 수량에 못 미치면 최소 수량으로 올린다.
"""

import sys
import time

from .live_feed import LiveShadowRunner
from .data_types import Side
from .binance_trader import BinanceFuturesTrader, TraderError


class LiveTradingRunner(LiveShadowRunner):
    def __init__(self, *args, trader: BinanceFuturesTrader = None,
                 trade_usdt: float = 100.0, notify=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.trader = trader
        self.trade_usdt = trade_usdt
        self.notify = notify or (lambda text: None)
        self.paused = False
        self.last_price = None
        self.started_at = time.time()

        # tag -> {"full_qty", "open_qty", "side"} 실행 수량 추적
        self._tags = {}
        self._n_exec_closed = 0

    # ---- 가격 추적 ------------------------------------------------------
    def on_agg_trade_msg(self, data):
        super().on_agg_trade_msg(data)
        if data.get("s") == self.symbol.upper():
            try:
                self.last_price = float(data["p"])
            except (KeyError, ValueError):
                pass

    # ---- 엔진 이벤트 -> 주문 미러링 --------------------------------------
    def _flush_new_logs(self):
        super()._flush_new_logs()
        try:
            self._sync_execution()
        except Exception as e:  # 실행 오류가 스트림 루프를 죽이면 안 된다
            print(f"[exec] error: {e}", file=sys.stderr)
            self._notify_safe(f"⚠️ 주문 동기화 오류: {e}")

    def _notify_safe(self, text):
        try:
            self.notify(text)
        except Exception as e:
            print(f"[notify] error: {e}", file=sys.stderr)

    def _sync_execution(self):
        self._exec_new_entries()
        self._exec_new_exits()

    def _exec_new_entries(self):
        for leg in self.engine.open_legs:
            if getattr(leg, "_exec_done", False):
                continue
            leg._exec_done = True
            t = leg.trade
            rec = self._tags.get(t.tag)
            if rec is None:
                full_qty = 0.0
                if self.trader:
                    full_qty = self.trader.round_qty(self.trade_usdt / t.entry_price)
                rec = {"full_qty": full_qty, "open_qty": 0.0, "side": t.side}
                self._tags[t.tag] = rec

            label = "LONG(매수)" if t.side == Side.LONG else "SHORT(매도)"
            head = f"Setup {t.setup} [{t.tag}] {label} 진입 @ {t.entry_price:,.1f} (트랜치 {t.qty_fraction:.0%})"

            if self.trader is None:
                self._notify_safe(f"📝 [PAPER] {head}")
                continue
            if self.paused:
                self._notify_safe(f"⏸ 일시정지 중 — 진입 건너뜀: {head}")
                continue

            qty = self.trader.round_qty(rec["full_qty"] * t.qty_fraction)
            min_q = self.trader.min_valid_qty(t.entry_price)
            if qty < min_q:
                qty = min_q
                self._notify_safe(f"ℹ️ 최소 주문수량 적용: {qty}")
            side = "BUY" if t.side == Side.LONG else "SELL"
            try:
                resp = self.trader.market_order(side, qty)
                filled = float(resp.get("executedQty", qty) or qty)
                rec["open_qty"] += filled
                avg = resp.get("avgPrice")
                px = f" (체결가 {float(avg):,.1f})" if avg and float(avg) > 0 else ""
                self._notify_safe(f"🟢 {head}\n실주문 체결: {side} {filled}{px}\nSL {leg.sl:,.1f} / TP1 {leg.tp1:,.1f} / TP2 {leg.tp2:,.1f}")
            except (TraderError, Exception) as e:
                self._notify_safe(f"🚨 진입 주문 실패: {head}\n{e}")

    def _exec_new_exits(self):
        while self._n_exec_closed < len(self.engine.closed_trades):
            t = self.engine.closed_trades[self._n_exec_closed]
            self._n_exec_closed += 1
            head = (f"Setup {t.setup} [{t.tag}] {t.reason} 청산 @ {t.exit_price:,.1f} "
                    f"({t.bps:+.1f}bps, R {t.r_multiple:+.2f})")

            rec = self._tags.get(t.tag)
            if self.trader is None or rec is None or rec["open_qty"] <= 0:
                self._notify_safe(f"📝 [PAPER] {head}")
                continue

            # 같은 태그의 레그가 아직 남아 있으면 부분 청산, 없으면 잔량 전부(더스트 방지)
            still_open = any(l.trade.tag == t.tag for l in self.engine.open_legs)
            if still_open:
                qty = self.trader.round_qty(rec["full_qty"] * t.qty_fraction)
                qty = min(qty, rec["open_qty"])
                if qty <= 0:
                    qty = rec["open_qty"]
            else:
                qty = rec["open_qty"]

            side = "SELL" if rec["side"] == Side.LONG else "BUY"
            try:
                resp = self.trader.market_order(side, qty, reduce_only=True)
                filled = float(resp.get("executedQty", qty) or qty)
                rec["open_qty"] = max(0.0, rec["open_qty"] - filled)
                emoji = "🔵" if (t.bps or 0) > 0 else "🔴"
                self._notify_safe(f"{emoji} {head}\n실주문 체결: {side} {filled} (reduceOnly)")
            except (TraderError, Exception) as e:
                self._notify_safe(f"🚨 청산 주문 실패: {head}\n{e}\n/close all 로 수동 청산을 검토하세요")

    # ---- 텔레그램 명령용 ------------------------------------------------
    def manual_close_all(self):
        """실포지션 전량 시장가 청산 + 엔진 가상 레그 정리."""
        resp = None
        if self.trader is not None:
            resp = self.trader.close_position()
        now = time.time()
        for leg in list(self.engine.open_legs):
            price = self.last_price or leg.trade.entry_price
            self.engine._close(leg, now, price, "MANUAL", leg.trade.qty_fraction)
        self.engine.open_legs = []
        self._n_exec_closed = len(self.engine.closed_trades)
        for rec in self._tags.values():
            rec["open_qty"] = 0.0
        return resp

    def status_summary(self) -> str:
        """텔레그램 /status 용 요약 문자열."""
        up_h = (time.time() - self.started_at) / 3600
        mode = "실거래" if self.trader else "페이퍼(주문 없음)"
        if self.trader and self.trader.testnet:
            mode += " · 테스트넷"
        lines = [
            f"모드: {mode}{' · ⏸ 일시정지' if self.paused else ''}",
            f"심볼: {self.symbol.upper()} / 가동 {up_h:.1f}h",
            f"현재가: {self.last_price:,.1f}" if self.last_price else "현재가: (수신 대기)",
            f"상태: SetupA={self.engine.a.state} / SetupB={self.engine.b.state}",
            f"보유 레그: {len(self.engine.open_legs)}개",
        ]
        for leg in self.engine.open_legs:
            t = leg.trade
            lines.append(f"  · {t.tag} {t.side.value} @ {t.entry_price:,.1f} "
                         f"(SL {leg.sl:,.1f} / TP1 {leg.tp1:,.1f} / TP2 {leg.tp2:,.1f})")
        s = self.engine.summary()
        for setup in ("A", "B"):
            st = s.get(setup)
            if st and st.get("count"):
                lines.append(f"Setup {setup}: {st['count']}건, 승률 {st['win_rate']*100:.0f}%, "
                             f"평균 {st['avg_bps']:+.1f}bps")
        if self.trader:
            try:
                pos = self.trader.get_position()
                bal = self.trader.get_usdt_balance()
                lines.append(f"거래소 포지션: {pos['qty']:+g} (미실현 {pos['unrealized']:+.2f} USDT)")
                lines.append(f"가용 잔고: {bal:,.2f} USDT")
            except Exception as e:
                lines.append(f"거래소 조회 실패: {e}")
        return "\n".join(lines)
