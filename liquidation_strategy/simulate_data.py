"""합성 데이터 생성기.

실거래소 접속 없이 명세서 5장 '섀도 단계' 검증을 재현하기 위한 것.
실제 청산 데이터의 통계적 성질(청산 캐스케이드의 두꺼운 꼬리, OI 유입-이탈
사이클)을 모사하는 합성 스트림을 만든다. 코드/알고리즘은 실데이터 연결 시
그대로 재사용 가능 — 이 파일만 실제 웹소켓/REST 어댑터로 교체하면 된다.

경고: 여기서 생성되는 수치는 전략 검증(사전등록 승률 45~60% 등)을 목표로
캘리브레이션된 것이 아니라, 파이프라인이 명세서의 트리거 정의를 정확히
탐지·집행하는지 확인하기 위한 스트레스 시나리오다.
"""

import numpy as np
from dataclasses import dataclass
from .data_types import Candle, ForceOrder, OIPoint


@dataclass
class SimResult:
    candles: list       # Candle, 1분 간격
    force_orders: list  # ForceOrder
    oi_points: list      # OIPoint, 5분 간격
    cvd_series: list     # (ts, delta) 1분 간격, on_cvd_delta 입력용
    box_series: list     # (ts, box_low, box_high) 4h 롤링 레인지
    events: list         # 주입된 이벤트 메타(정답 라벨), 디버그/검증용


def generate(days: int = 120, seed: int = 7, start_price: float = 62000.0) -> SimResult:
    rng = np.random.default_rng(seed)
    minutes = days * 24 * 60
    t0 = 1_750_000_000.0  # 임의 기준 epoch
    ts = t0 + np.arange(minutes) * 60.0

    # --- 기본 랜덤워크 (분당 변동성 ~6bps, 완만한 평균회귀) ---
    price = np.empty(minutes)
    price[0] = start_price
    mu, sigma = 0.0, 0.0006
    for i in range(1, minutes):
        shock = rng.normal(mu, sigma)
        price[i] = price[i - 1] * (1 + shock)

    force_orders = []  # 트리거엔 더 이상 안 쓰이지만(setup_a.py 재설계), 원시 이벤트
                        # 볼륨 참고용으로 계속 생성해둔다 — 실데이터의 raw_events.jsonl과
                        # 같은 성격.
    events = []
    cvd = np.zeros(minutes)  # 분당 CVD 델타 (기본 노이즈)
    cvd += rng.normal(0, 0.3, minutes)

    # 시간당 평균 청산 명목가(레거시 리포트 표기용 통계치 — 더 이상 트리거에 안 쓰임)
    baseline_notional_per_hour = 250_000.0

    # 거래량 배율(RVOL 게이트용) — 기본 1.0, 캐스케이드 구간에서만 폭증시킨다
    vol_mult = np.ones(minutes)

    # OI 배열 — Setup A(급감)와 Setup B(급증) 둘 다 같은 배열에 기록해야 하므로
    # 두 주입 루프보다 먼저 만든다.
    oi_base = 180_000.0
    n_oi = minutes // 5 + 1
    oi_noise = rng.normal(0, 1500, n_oi)
    oi = np.full(n_oi, oi_base) + np.cumsum(oi_noise) * 0.02
    oi = np.clip(oi, oi_base * 0.6, oi_base * 1.8)

    # ------------------------------------------------------------------
    # Setup A용: 청산 캐스케이드 주입 (주 3~10회 목표 -> 총 events ~= days/7*6)
    # 가격 급락 + OI 급감(디레버리징) + 거래량 폭증(RVOL) 세 가지를 함께 주입한다 —
    # setup_a.py 재설계 후 T1 트리거가 이 세 조건을 캔들+OI 스트림만으로 판정하므로.
    n_cascades = max(1, int(days / 7 * 6))
    cascade_starts = rng.choice(np.arange(120, minutes - 200), size=n_cascades, replace=False)
    for start in sorted(cascade_starts):
        depth = rng.uniform(0.009, 0.028)          # 하락폭 0.9~2.8%
        cascade_len_min = int(rng.uniform(2, 6))     # 캐스케이드 지속 2~6분
        recover_frac = rng.uniform(0.2, 0.95)         # 소진 후 되돌림 비율 (승패 좌우)
        recover_len_min = int(rng.uniform(5, 60))

        base = price[start]
        low = base * (1 - depth)
        # 하락 구간
        for k in range(cascade_len_min):
            frac = (k + 1) / cascade_len_min
            price[start + k] = base * (1 - depth * frac)
        cascade_low_idx = start + cascade_len_min - 1

        # 60초 윈도우 안에 3건 이상 SELL 청산, 합계 >= baseline*8 되도록 생성
        n_liqs = rng.integers(3, 9)
        liq_ts = ts[start] + np.sort(rng.uniform(0, 55, n_liqs))
        total_target = baseline_notional_per_hour * rng.uniform(8.5, 20)
        weights = rng.dirichlet(np.ones(n_liqs))
        for j, lt in enumerate(liq_ts):
            notional = total_target * weights[j]
            p = base * (1 - depth * min(1.0, (j + 1) / n_liqs))
            qty = notional / p
            force_orders.append(ForceOrder(ts=float(lt), side="SELL", price=float(p), qty=float(qty)))

        # 거래량 폭증(RVOL 게이트용) — 캐스케이드 구간 거래량을 크게 키운다
        vol_mult[start:start + cascade_len_min] *= rng.uniform(4.0, 10.0)

        # OI 급감(디레버리징 확인, DeltaOI% 게이트용) — 캐스케이드 구간에 OI를 낮춘다
        oi_drop_frac = rng.uniform(0.03, 0.08)
        start5 = start // 5
        end5 = min((start + cascade_len_min + 4) // 5 + 1, len(oi))
        if start5 < len(oi):
            oi[start5:end5] *= (1 - oi_drop_frac)

        # CVD: 캐스케이드 동안 강하게 음수, 소진 후 0 이상으로 전환
        cvd[start:start + cascade_len_min] -= rng.uniform(3, 8, cascade_len_min)

        # 소진 확인 게이트: 마지막 청산 후 90초(=바로 다음 분봉 이후) 무청산, 반등 유지
        exhaust_idx = cascade_low_idx + 2  # 90s ~ 2분 뒤 근사
        if exhaust_idx < minutes:
            cvd[exhaust_idx] = rng.uniform(0.5, 5)   # CVD 반전

        # 되돌림 구간 반영
        recover_target = low + recover_frac * (base - low)
        for k in range(recover_len_min):
            if cascade_low_idx + 1 + k >= minutes:
                break
            frac = (k + 1) / recover_len_min
            idx = cascade_low_idx + 1 + k
            price[idx] = low + frac * (recover_target - low)

        events.append({
            "type": "A",
            "start_ts": float(ts[start]),
            "low_ts": float(ts[cascade_low_idx]),
            "low_price": float(low),
            "start_price": float(base),
            "recover_frac": float(recover_frac),
        })

    # ------------------------------------------------------------------
    # Setup B용: 트랩드롱 플러시 주입 (주 1~4회 목표). oi 배열은 위에서 이미 만들어짐
    # (Setup A 캐스케이드의 OI 급감이 먼저 적용된 같은 배열 위에 이어서 기록한다).
    n_traps = max(1, int(days / 7 * 2.5))
    trap_starts = rng.choice(np.arange(300, minutes - 600), size=n_traps, replace=False)
    for start in sorted(trap_starts):
        box_high = float(np.max(price[max(0, start - 240):start]) * 1.0005)
        box_low = float(np.min(price[max(0, start - 240):start]) * 0.9995)

        breakout_pct = rng.uniform(0.003, 0.012)
        sweep_high = box_high * (1 + breakout_pct)
        price[start] = sweep_high

        oi_bump = rng.uniform(0.018, 0.05)
        oi5_idx = start // 5
        if oi5_idx < len(oi):
            oi[oi5_idx:min(oi5_idx + 6, len(oi))] += oi[oi5_idx] * oi_bump

        # 실패: 15분 내 박스 상단 아래로 복귀
        fail_len = int(rng.uniform(3, 12))
        for k in range(fail_len):
            idx = start + 1 + k
            if idx >= minutes:
                break
            frac = (k + 1) / fail_len
            price[idx] = sweep_high - frac * (sweep_high - box_high * 0.998)

        retest_fail = rng.random() < 0.6
        flush_start = start + fail_len + 1
        if retest_fail and flush_start < minutes:
            # 리테스트 실패 후 청산 클러스터까지 플러시
            liq_target = price[flush_start - 1] * (1 - rng.uniform(0.02, 0.07))
            flush_len = int(rng.uniform(20, 180))
            for k in range(flush_len):
                idx = flush_start + k
                if idx >= minutes:
                    break
                frac = (k + 1) / flush_len
                price[idx] = price[flush_start - 1] + frac * (liq_target - price[flush_start - 1])
            # OI 청산으로 감소
            end5 = min((flush_start + flush_len) // 5, len(oi) - 1)
            if end5 > oi5_idx:
                oi[oi5_idx + 6:end5 + 1] *= rng.uniform(0.85, 0.97)

        events.append({
            "type": "B",
            "start_ts": float(ts[start]),
            "box_high": box_high,
            "box_low": box_low,
            "sweep_high": float(sweep_high),
            "retest_fail": bool(retest_fail),
        })

    # ------------------------------------------------------------------
    # OHLC 근사 (1분봉이므로 open=이전 close, close=현재, high/low는 노이즈로 확장).
    # 상단/하단 윅을 독립적으로 뽑는다 — 같은 값을 공유하면 wick_ratio가 항상
    # 정확히 1.0으로 고정되어(setup_a.py T3의 반전캔들 판정이 절대 안 통과) 실제
    # 캔들의 상하 비대칭을 전혀 반영하지 못한다.
    opens = np.concatenate([[price[0]], price[:-1]])
    upper_wick = np.abs(rng.normal(0, 0.0004, minutes)) * price
    lower_wick = np.abs(rng.normal(0, 0.0004, minutes)) * price
    highs = np.maximum(price, opens) + upper_wick
    lows = np.minimum(price, opens) - lower_wick
    vols = np.abs(rng.normal(50, 15, minutes)) * vol_mult

    candles = [
        Candle(ts=float(ts[i]), open=float(opens[i]), high=float(highs[i]),
               low=float(lows[i]), close=float(price[i]), volume=float(vols[i]),
               cvd_delta=float(cvd[i]))
        for i in range(minutes)
    ]
    cvd_series = [(float(ts[i]), float(cvd[i])) for i in range(minutes)]

    oi_ts = t0 + np.arange(len(oi)) * 300.0
    oi_points = [OIPoint(ts=float(oi_ts[i]), oi=float(oi[i])) for i in range(len(oi))]

    # 4h 롤링 박스 (240분) — "이번 봉 이전까지"의 고가/저가로 계산한다(live_feed.py와
    # 동일 원칙: 이번 봉을 포함해서 계산하면 새 극값이 곧 박스 경계가 되어버려
    # 그 봉 자신의 돌파를 절대 감지할 수 없다). close 대신 실제 윅(high/low)을
    # 써야 라이브 박스 계산과 일치한다 — close만 쓰면 wick 돌파를 놓친다.
    box_series = []
    for i in range(minutes):
        lo = max(0, i - 240)
        if i == 0:
            box_series.append((float(ts[i]), float(lows[i]), float(highs[i])))
        else:
            box_series.append((float(ts[i]), float(np.min(lows[lo:i])), float(np.max(highs[lo:i]))))

    events.sort(key=lambda e: e["start_ts"])
    return SimResult(candles, force_orders, oi_points, cvd_series, box_series, events), baseline_notional_per_hour
