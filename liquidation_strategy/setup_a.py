"""Setup A — 청산 캐스케이드 소진 롱 (Cascade Exhaustion Long)

명세서 1장의 트리거(T1~T4)·실행 규칙을 순서대로 구현한 이벤트 기반 상태 머신.
스트리밍 데이터(ForceOrder, Candle)를 시간순으로 하나씩 `on_force_order` /
`on_candle` 에 흘려 넣으면 내부적으로 캐스케이드를 추적하고, 소진 확인 시
`pending_signal` 에 진입 신호를 채운다.
"""

from dataclasses import dataclass, field
from collections import deque
from .data_types import ForceOrder, Candle, Trade, Side


@dataclass
class CascadeAParams:
    lookback_hours: float = 24.0        # 기준 시간당 평균 산정 구간
    vol_multiplier: float = 3.0          # T1: 60s 합계 >= 시간당 평균 * N (구 8.0 — 발동 빈도 완화)
    min_chain: int = 2                   # T1: 최소 연쇄 건수 (구 3)
    cascade_window_s: float = 60.0       # T1: 감지 윈도우
    min_move_pct: float = 0.004          # T2: 캐스케이드 시작 대비 하락률 (구 0.008)
    exhaustion_gap_s: float = 45.0       # T3: 마지막 청산 후 무청산 경과 (구 90)
    cvd_window_s: float = 60.0           # T3: 1분 CVD
    rebound_pct: float = 0.0008          # T3: 저점 대비 반등폭 (구 0.0015)
    rebound_hold_s: float = 15.0         # T3: 반등 유지 시간 (구 30)
    sl_buffer_pct: float = 0.0015        # 손절 = 저점 - 버퍼
    tp1_retrace: float = 0.38            # 되돌림 38%
    tp2_retrace: float = 0.618           # 되돌림 61.8%
    tp1_fraction: float = 0.5            # TP1에서 청산할 비율
    time_exit_s: float = 90 * 60         # 시간 청산 (90분)
    max_reentries: int = 1               # 동일 캐스케이드 재진입 허용 횟수


class CascadeExhaustionLong:
    """T1~T4 감지 + 포지션 관리 상태 머신."""

    def __init__(self, params: CascadeAParams = None, macro_blackouts=None):
        self.p = params or CascadeAParams()
        # macro_blackouts: [(start_ts, end_ts), ...]  FOMC/CPI ±30분
        self.macro_blackouts = macro_blackouts or []

        self._hourly_baseline = None       # 시간당 평균 청산 금액(외부 주입)
        self._sell_liqs = deque()          # (ts, notional) 최근 청산 로그
        self._cvd_buf = deque()            # (ts, delta) 1분 CVD 롤링

        self.state = "IDLE"                # IDLE -> CASCADE -> WATCH_EXHAUST -> ARMED
        self.cascade_start_ts = None
        self.cascade_start_price = None
        self.cascade_low = None
        self.cascade_low_ts = None
        self.last_liq_ts = None
        self.rebound_since_ts = None
        self.reentry_count = 0
        self.cascade_id = 0

        self.pending_signal = None         # {"ts", "price", "tag"} 진입 신호
        self.log = []

    def set_hourly_baseline(self, notional_per_hour: float):
        self._hourly_baseline = notional_per_hour

    def _in_macro_blackout(self, ts: float) -> bool:
        return any(s <= ts <= e for s, e in self.macro_blackouts)

    def _prune(self, ts: float):
        while self._sell_liqs and ts - self._sell_liqs[0][0] > self.p.cascade_window_s:
            self._sell_liqs.popleft()
        while self._cvd_buf and ts - self._cvd_buf[0][0] > self.p.cvd_window_s:
            self._cvd_buf.popleft()

    def on_cvd_delta(self, ts: float, delta: float):
        self._cvd_buf.append((ts, delta))
        self._prune(ts)

    def _cvd_1m(self) -> float:
        return sum(d for _, d in self._cvd_buf)

    def on_force_order(self, fo: ForceOrder):
        if fo.side != "SELL":
            return  # 롱 청산(SELL)만 하방 캐스케이드 후보
        self._prune(fo.ts)
        self._sell_liqs.append((fo.ts, fo.notional))
        self.last_liq_ts = fo.ts

        window_sum = sum(n for _, n in self._sell_liqs)
        window_count = len(self._sell_liqs)

        if self.state == "IDLE":
            if (
                self._hourly_baseline
                and window_sum >= self._hourly_baseline * self.p.vol_multiplier
                and window_count >= self.p.min_chain
            ):
                self.cascade_id += 1
                self.state = "CASCADE"
                self.cascade_start_ts = fo.ts
                self.cascade_start_price = fo.price
                self.cascade_low = fo.price
                self.cascade_low_ts = fo.ts
                self.reentry_count = 0
                self.log.append((fo.ts, f"T1 캐스케이드 감지 (id={self.cascade_id}, sum={window_sum:.0f})"))
        elif self.state in ("CASCADE", "WATCH_EXHAUST"):
            if fo.price < self.cascade_low:
                self.cascade_low = fo.price
                self.cascade_low_ts = fo.ts
            # 신규 청산 발생 -> 소진 관찰 리셋
            self.state = "CASCADE"
            self.rebound_since_ts = None

            move = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price
            if move >= self.p.min_move_pct:
                self.state = "WATCH_EXHAUST"
                self.log.append((fo.ts, f"T2 가격이탈 확인 move={move*100:.2f}%"))

    def on_candle(self, c: Candle):
        """가격/CVD 스트림 tick. T2 재평가(캔들 저가 기준)와 T3 소진 판정,
        열린 포지션 관리가 여기서 이뤄진다."""
        if self.state in ("CASCADE", "WATCH_EXHAUST"):
            # forceOrder는 1000ms당 대표 1건만 전송되므로(명세 4장 주의),
            # 캔들 저가로도 캐스케이드 저점/T2를 갱신해야 짧은 캐스케이드를 놓치지 않는다.
            if c.low < self.cascade_low:
                self.cascade_low = c.low
                self.cascade_low_ts = c.ts
            move = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price
            if self.state == "CASCADE" and move >= self.p.min_move_pct:
                self.state = "WATCH_EXHAUST"
                self.log.append((c.ts, f"T2 가격이탈 확인(캔들) move={move*100:.2f}%"))

        if self.state == "WATCH_EXHAUST" and self.last_liq_ts is not None:
            gap_ok = (c.ts - self.last_liq_ts) >= self.p.exhaustion_gap_s
            cvd_ok = self._cvd_1m() >= 0
            rebound_pct_now = (c.close - self.cascade_low) / self.cascade_low

            if rebound_pct_now >= self.p.rebound_pct:
                if self.rebound_since_ts is None:
                    self.rebound_since_ts = c.ts
            else:
                self.rebound_since_ts = None

            rebound_hold_ok = (
                self.rebound_since_ts is not None
                and (c.ts - self.rebound_since_ts) >= self.p.rebound_hold_s
            )

            if gap_ok and cvd_ok and rebound_hold_ok:
                if self._in_macro_blackout(c.ts):
                    self.log.append((c.ts, "T4 매크로 블랙아웃 - 진입 차단"))
                    self.state = "IDLE"
                    return
                self.pending_signal = {
                    "ts": c.ts,
                    "price": c.close,
                    "cascade_low": self.cascade_low,
                    "cascade_start_price": self.cascade_start_price,
                    "tag": f"A-{self.cascade_id}",
                }
                self.log.append((c.ts, f"T3 소진 확인 -> 진입 신호 @ {c.close:.1f}"))
                self.state = "ARMED"

    def consume_signal(self):
        sig = self.pending_signal
        self.pending_signal = None
        return sig

    def describe(self):
        """현재 상태를 사람이 읽을 텍스트로 설명 (GUI/텔레그램 표시용).

        반환: (설명 텍스트, 참고 가격 또는 None). 참고 가격은 차트에
        점선으로 그릴 때 쓰는 '지금 전략이 주시 중인 레벨'이다.
        """
        p = self.p
        if self.state == "IDLE":
            return (
                f"청산 캐스케이드 대기 중 — 60초 내 SELL청산 합계가 "
                f"시간당평균×{p.vol_multiplier:.1f} 이상 & {p.min_chain}건 이상 발생하면 추적 시작",
                None,
            )
        if self.state == "CASCADE":
            move_pct = 0.0
            if self.cascade_start_price:
                move_pct = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price * 100
            return (
                f"캐스케이드 진행 중(#{self.cascade_id}) — 저점 {self.cascade_low:,.1f} "
                f"(시작가 대비 -{move_pct:.2f}%). -{p.min_move_pct*100:.2f}% 하락 확인되면 소진 관찰 시작",
                self.cascade_low,
            )
        if self.state == "WATCH_EXHAUST":
            return (
                f"하락 소진 확인 중 — 저점 {self.cascade_low:,.1f} 대비 +{p.rebound_pct*100:.2f}% 반등이 "
                f"{p.rebound_hold_s:.0f}초 유지 + 무청산 {p.exhaustion_gap_s:.0f}초 경과 + CVD 양전환 시 매수 진입",
                self.cascade_low,
            )
        if self.state == "ARMED":
            return "진입 신호 발생 — 체결 대기 중", self.cascade_low
        return self.state, None

    def _window_stats(self, now_ts: float):
        """now_ts 기준으로 60초 청산 윈도우를 다시 계산한다 (상태를 바꾸지
        않는 읽기 전용 조회 — 표시용으로 정확한 실시간 값을 주기 위함,
        _sell_liqs 자체는 on_force_order가 들어올 때만 정리되므로 그 사이엔
        오래된 값이 남아있을 수 있다)."""
        cutoff = now_ts - self.p.cascade_window_s
        items = [n for ts, n in self._sell_liqs if ts >= cutoff]
        return sum(items), len(items)

    def conditions(self, now_ts: float):
        """T1~T4 하위 조건 각각의 실시간 충족 여부 (GUI 체크리스트 표시용).

        상태 단계와 무관하게 항상 전부 계산한다 — 예를 들어 IDLE이어도 T1의
        진행 상황을, WATCH_EXHAUST에서도 T1/T2는 이미 충족된 값 그대로 보여준다.
        반환: [{"key", "label", "met": bool, "detail": str}, ...] (7개 고정).
        """
        p = self.p
        out = []

        window_sum, window_count = self._window_stats(now_ts)
        threshold = self._hourly_baseline * p.vol_multiplier if self._hourly_baseline else None
        out.append({
            "key": "t1_vol",
            "label": f"T1: 60초 청산합계 ≥ 기준×{p.vol_multiplier:.1f}",
            "met": threshold is not None and window_sum >= threshold,
            "detail": f"{window_sum:,.0f} / {threshold:,.0f}" if threshold else f"{window_sum:,.0f} / 기준선 대기중",
        })
        out.append({
            "key": "t1_chain",
            "label": f"T1: 최소 연쇄 {p.min_chain}건",
            "met": window_count >= p.min_chain,
            "detail": f"{window_count}건",
        })

        if self.cascade_start_price and self.cascade_low is not None:
            move_pct = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price
        else:
            move_pct = 0.0
        out.append({
            "key": "t2_move",
            "label": f"T2: 하락률 ≥ {p.min_move_pct*100:.2f}%",
            "met": move_pct >= p.min_move_pct,
            "detail": f"{move_pct*100:.2f}%",
        })

        gap = (now_ts - self.last_liq_ts) if self.last_liq_ts is not None else None
        out.append({
            "key": "t3_gap",
            "label": f"T3: 무청산 경과 ≥ {p.exhaustion_gap_s:.0f}초",
            "met": gap is not None and gap >= p.exhaustion_gap_s,
            "detail": f"{gap:.0f}초 경과" if gap is not None else "청산 이력 없음",
        })
        cvd = self._cvd_1m()
        out.append({
            "key": "t3_cvd",
            "label": "T3: 1분 CVD ≥ 0",
            "met": cvd >= 0,
            "detail": f"{cvd:+.2f}",
        })
        hold = (now_ts - self.rebound_since_ts) if self.rebound_since_ts is not None else 0.0
        out.append({
            "key": "t3_rebound",
            "label": f"T3: 반등 {p.rebound_pct*100:.2f}% 유지 ≥ {p.rebound_hold_s:.0f}초",
            "met": self.rebound_since_ts is not None and hold >= p.rebound_hold_s,
            "detail": f"{hold:.0f}초 유지 중" if self.rebound_since_ts is not None else "반등 미확인",
        })
        blackout = self._in_macro_blackout(now_ts)
        out.append({
            "key": "t4_macro",
            "label": "T4: 매크로 블랙아웃 아님",
            "met": not blackout,
            "detail": "차단 구간" if blackout else "정상",
        })
        return out

    def allow_reentry(self) -> bool:
        return self.reentry_count < self.p.max_reentries

    def register_reentry(self):
        self.reentry_count += 1
        self.state = "WATCH_EXHAUST"
        self.rebound_since_ts = None

    def reset(self):
        """탐지 상태만 IDLE로 되돌린다. cascade_low/start_price 등은 재진입
        판단(register_reentry)에 쓰일 수 있으므로 남겨둔다."""
        self.state = "IDLE"
        self.rebound_since_ts = None

    @staticmethod
    def compute_exits(entry_price: float, cascade_low: float, cascade_start_price: float, p: CascadeAParams):
        move = cascade_start_price - cascade_low
        sl = cascade_low * (1 - p.sl_buffer_pct)
        tp1 = cascade_low + p.tp1_retrace * move
        tp2 = cascade_low + p.tp2_retrace * move
        return sl, tp1, tp2
