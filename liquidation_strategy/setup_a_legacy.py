"""Setup A(레거시) — forceOrder(청산 틱) 기반 캐스케이드 소진 롱.

[배경] setup_a.py는 fstream.binance.com의 forceOrder 프레임이 안 들어오는
문제(당시 지역 차단으로 추정) 때문에 캔들+거래량+OI만으로 재설계됐다(커밋
793c94e). 나중에 실제 원인이 밝혀졌는데 — Binance가 2026-04-23부로 레거시
wss://fstream.binance.com/stream 을 public/market/private 세 엔드포인트로
분리하면서, forceOrder를 포함한 "market" 카테고리 스트림 전체가 레거시
URL에서 조용히 끊긴 것이었다(지역과 무관). live_feed.py의 STREAM_URL을
/market/stream으로 이전해 이 문제 자체는 해결됐고, 지금은 run_all.py/
run_bot.py 둘 다 forceOrder를 정상 수신한다.

그럼에도 이 파일을 run_bot.py 전용으로 유지하는 건 데이터 가용성 때문이
아니라 순수한 선택이다 — run_bot.py는 forceOrder(청산 틱) 기반 원본
구현을, run_all.py는 setup_a.py의 캔들+거래량+OI 재설계 버전을 그대로 쓴다.

로직은 793c94e 이전 커밋의 setup_a.py를 그대로 가져온 것이다(T1: 60초 내
SELL청산 합계가 최근 24시간 시간당 평균×배율 이상 & 최소 연쇄건수 이상,
T3: 무청산 경과+CVD 양전환+반등유지). 원래는 RollingBaseline을
live_feed.py가 별도로 관리하며 set_hourly_baseline()으로 주입했지만, 여기서는
on_force_order() 안에 캡슐화해 engine.py/live_feed.py가 이 구현의 내부
사정을 몰라도 되게 했다 — 계산 자체는 원본과 동일하다.

engine.py의 StrategyEngine과 맞물리기 위해 setup_a.py의 새 CascadeExhaustionLong과
공개 인터페이스를 맞췄다(원본 대비 차이):
  - on_candle(c, oi_now=None): oi_now는 안 쓰지만 시그니처 호환을 위해 받는다.
  - conditions(now_ts, current_price=None): current_price는 안 쓰지만 받는다.
  - compute_exits(..., side="long"): 원래도 롱 전용이었으므로 side는 항상
    "long"이어야 하며, engine.py가 다른 셋업과 동일하게 `type(self.a).
    compute_exits(...)`로 다형 호출할 수 있도록 인자 이름만 맞췄다.
  - pending_signal 딕셔너리 키를 setup_a.py와 동일하게 "side"(항상 "long")와
    "cascade_extreme"(원래 이름은 "cascade_low")으로 맞췄다 —
    engine.py의 _open_a()가 이 키 이름으로 읽는다.
"""

from collections import deque
from dataclasses import dataclass
from .data_types import ForceOrder, Candle

BASELINE_WINDOW_S = 24 * 3600


@dataclass
class LegacyCascadeAParams:
    lookback_hours: float = 24.0        # 기준 시간당 평균 산정 구간
    vol_multiplier: float = 3.0          # T1: 60s 합계 >= 시간당 평균 * N
    min_chain: int = 2                   # T1: 최소 연쇄 건수
    cascade_window_s: float = 60.0       # T1: 감지 윈도우
    min_move_pct: float = 0.004          # T2: 캐스케이드 시작 대비 하락률
    exhaustion_gap_s: float = 45.0       # T3: 마지막 청산 후 무청산 경과
    cvd_window_s: float = 60.0           # T3: 1분 CVD
    rebound_pct: float = 0.0008          # T3: 저점 대비 반등폭
    rebound_hold_s: float = 15.0         # T3: 반등 유지 시간
    sl_buffer_pct: float = 0.0015        # 손절 = 저점 - 버퍼
    tp1_retrace: float = 0.38            # 되돌림 38%
    tp2_retrace: float = 0.618           # 되돌림 61.8%
    tp1_fraction: float = 0.5            # TP1에서 청산할 비율
    time_exit_s: float = 90 * 60         # 시간 청산 (90분)
    max_reentries: int = 1               # 동일 캐스케이드 재진입 허용 횟수


class RollingBaseline:
    """T1 임계값 비교용 '최근 24h 시간당 평균 청산 금액'을 라이브로 추정.

    Binance forceOrder는 공개 과거 이력 REST가 없어(binance_client.py 참고),
    이 베이스라인은 실시간 forceOrder 틱이 쌓이는 만큼만 정확해진다 — 봇을
    막 시작한 직후에는 표본이 적어 T1이 거의 발동하지 않는 것이 정상이다."""

    def __init__(self, window_s=BASELINE_WINDOW_S):
        self.window_s = window_s
        self.buf = deque()  # (ts, notional)

    def add(self, ts, notional):
        self.buf.append((ts, notional))
        self._prune(ts)

    def _prune(self, ts):
        while self.buf and ts - self.buf[0][0] > self.window_s:
            self.buf.popleft()

    def per_hour(self, ts):
        self._prune(ts)
        if not self.buf:
            return None
        span_h = max(1.0, (ts - self.buf[0][0]) / 3600.0)
        total = sum(n for _, n in self.buf)
        return total / span_h


class LegacyCascadeExhaustionLong:
    """T1~T4 감지 + 포지션 관리 상태 머신 (forceOrder 기반, 롱 전용)."""

    def __init__(self, params: LegacyCascadeAParams = None, macro_blackouts=None):
        self.p = params or LegacyCascadeAParams()
        # macro_blackouts: [(start_ts, end_ts), ...]  FOMC/CPI ±30분
        self.macro_blackouts = macro_blackouts or []

        self._baseline = RollingBaseline(window_s=self.p.lookback_hours * 3600)
        self._hourly_baseline = None
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

        self.pending_signal = None         # {"ts", "price", "side", "cascade_extreme", ...}
        self.log = []

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
        self._baseline.add(fo.ts, fo.notional)
        self._hourly_baseline = self._baseline.per_hour(fo.ts)

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

    def on_candle(self, c: Candle, oi_now: float = None):
        """가격/CVD 스트림 tick. T2 재평가(캔들 저가 기준)와 T3 소진 판정,
        열린 포지션 관리가 여기서 이뤄진다. oi_now는 setup_a.py와의 시그니처
        호환용으로만 받는다 — 이 구현은 OI를 쓰지 않는다(forceOrder 전용)."""
        if self.state in ("CASCADE", "WATCH_EXHAUST"):
            # forceOrder는 1000ms당 대표 1건만 전송되므로, 캔들 저가로도
            # 캐스케이드 저점/T2를 갱신해야 짧은 캐스케이드를 놓치지 않는다.
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
                    "side": "long",
                    "cascade_extreme": self.cascade_low,
                    "cascade_start_price": self.cascade_start_price,
                    "tag": f"A-{self.cascade_id}",
                }
                self.log.append((c.ts, f"T3 소진 확인 -> 진입 신호 @ {c.close:.1f}"))
                self.state = "ARMED"

    def consume_signal(self):
        sig = self.pending_signal
        self.pending_signal = None
        return sig

    def describe(self, current_price: float = None):
        """현재 상태를 사람이 읽을 텍스트로 설명 (GUI/텔레그램 표시용).
        current_price는 setup_a.py와의 시그니처 호환용으로만 받는다.

        반환: (설명 텍스트, 참고 가격 또는 None)."""
        p = self.p
        if self.state == "IDLE":
            return (
                f"청산 캐스케이드 대기 중(forceOrder) — 60초 내 SELL청산 합계가 "
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
        않는 읽기 전용 조회 — 표시용으로 정확한 실시간 값을 주기 위함)."""
        cutoff = now_ts - self.p.cascade_window_s
        items = [n for ts, n in self._sell_liqs if ts >= cutoff]
        return sum(items), len(items)

    def conditions(self, now_ts: float, current_price: float = None):
        """T1~T4 하위 조건 각각의 실시간 충족 여부 (GUI/텔레그램 체크리스트 표시용).
        current_price는 setup_a.py와의 시그니처 호환용으로만 받는다 — 가격은
        forceOrder 틱 자체(fo.price)로 이미 추적하므로 여기선 안 쓴다.

        반환: [{"key", "label", "met": bool, "detail": str}, ...] (7개 고정)."""
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
    def compute_exits(entry_price: float, cascade_extreme: float, cascade_start_price: float,
                       p: LegacyCascadeAParams, side: str = "long"):
        """롱 전용(forceOrder는 SELL청산=하방 캐스케이드만 추적). side 인자는
        engine.py가 setup_a.py의 CascadeExhaustionLong.compute_exits와 동일한
        방식(type(self.a).compute_exits(..., side=sig["side"]))으로 다형 호출할
        수 있도록 시그니처만 맞춘 것 — "long" 외의 값은 들어오지 않는다."""
        move = cascade_start_price - cascade_extreme
        sl = cascade_extreme * (1 - p.sl_buffer_pct)
        tp1 = cascade_extreme + p.tp1_retrace * move
        tp2 = cascade_extreme + p.tp2_retrace * move
        return sl, tp1, tp2
