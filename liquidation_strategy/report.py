"""엔진 결과 -> 대시보드용 JSON 스키마 변환 (합성/실데이터 파이프라인 공용).

`run.py`(합성 데이터)와 `run_live.py`(실데이터 라이브)가 동일한 스키마를
만들도록 여기 한 곳에 모은다. `build_dashboard.py`는 이 스키마만 알면 된다.
"""


def rejection_check_a(stats):
    checks = []
    count = stats.get("count", 0)
    avg_bps = stats.get("avg_bps", 0)
    win_rate = stats.get("win_rate", 0)
    max_cl = stats.get("max_consec_losses", 0)

    checks.append({
        "rule": "30건 도달 시 비용후 평균 <= 0bps",
        "applicable": count >= 30,
        "value": f"{count}건 / 평균 {avg_bps:.1f}bps",
        "failed": count >= 30 and avg_bps <= 0,
    })
    checks.append({
        "rule": "50건 도달 시 평균 < +8bps 또는 승률 < 35%",
        "applicable": count >= 50,
        "value": f"{count}건 / 평균 {avg_bps:.1f}bps / 승률 {win_rate*100:.1f}%",
        "failed": count >= 50 and (avg_bps < 8 or win_rate < 0.35),
    })
    checks.append({
        "rule": "최대 연속 손실 10회 초과",
        "applicable": True,
        "value": f"{max_cl}회",
        "failed": max_cl > 10,
    })
    rejected = any(c["failed"] for c in checks)
    return checks, rejected


def rejection_check_b(stats):
    checks = []
    count = stats.get("count", 0)
    avg_bps = stats.get("avg_bps", 0)
    win_rate = stats.get("win_rate", 0)
    avg_r = stats.get("avg_r", 0)

    checks.append({
        "rule": "20건 도달 시 비용후 평균 <= 0bps",
        "applicable": count >= 20,
        "value": f"{count}건 / 평균 {avg_bps:.1f}bps",
        "failed": count >= 20 and avg_bps <= 0,
    })
    checks.append({
        "rule": "40건 도달 시 승률 < 30% 또는 평균 R < 1.2",
        "applicable": count >= 40,
        "value": f"{count}건 / 승률 {win_rate*100:.1f}% / 평균 R {avg_r:.2f}",
        "failed": count >= 40 and (win_rate < 0.30 or avg_r < 1.2),
    })
    rejected = any(c["failed"] for c in checks)
    return checks, rejected


def trade_to_dict(t):
    return {
        "setup": t.setup,
        "side": t.side.value,
        "entry_ts": t.entry_ts,
        "entry_price": round(t.entry_price, 2),
        "exit_ts": t.exit_ts,
        "exit_price": round(t.exit_price, 2) if t.exit_price else None,
        "qty_fraction": round(t.qty_fraction, 3),
        "reason": t.reason,
        "r_multiple": round(t.r_multiple, 3) if t.r_multiple is not None else None,
        "bps": round(t.bps, 2) if t.bps is not None else None,
        "tag": t.tag,
    }


def build_equity_curve(trades):
    curve = []
    cum = 0.0
    for i, t in enumerate(sorted(trades, key=lambda x: x.exit_ts)):
        cum += t.bps * t.qty_fraction
        curve.append({"n": i + 1, "ts": t.exit_ts, "cum_bps": round(cum, 2), "setup": t.setup})
    return curve


def downsample_price(candles, step_minutes=60):
    return [
        {"ts": c.ts, "close": round(c.close, 1)}
        for i, c in enumerate(candles)
        if i % step_minutes == 0
    ]


def sample_window(candles, center_ts, before_min=90, after_min=180):
    if not candles:
        return []
    center_idx = min(range(len(candles)), key=lambda i: abs(candles[i].ts - center_ts))
    lo = max(0, center_idx - before_min)
    hi = min(len(candles), center_idx + after_min)
    return [
        {"ts": c.ts, "open": round(c.open, 1), "high": round(c.high, 1),
         "low": round(c.low, 1), "close": round(c.close, 1)}
        for c in candles[lo:hi]
    ]


def build_report(engine, candles, meta, optimized_params=None, grid_search=None):
    """StrategyEngine 결과 + 캔들 히스토리 -> 대시보드 JSON 스키마.

    optimized_params를 명시하지 않으면 엔진이 실제로 사용 중인 파라미터
    (engine.a.p / engine.b.p)에서 그대로 뽑아낸다 — 합성 그리드서치 없이
    실행되는 live_feed.py / backfill.py 에서도 트리거 로직 다이어그램에
    항상 실제 임계값이 표시되도록 하기 위함이다.
    """
    if optimized_params is None:
        optimized_params = {"A": dict(engine.a.p.__dict__), "B": dict(engine.b.p.__dict__)}

    summary = engine.summary()
    a_checks, a_rejected = rejection_check_a(summary.get("A", {}))
    b_checks, b_rejected = rejection_check_b(summary.get("B", {}))

    trades = sorted(engine.closed_trades, key=lambda t: t.exit_ts)
    equity_curve = build_equity_curve(trades)

    a_trades = [t for t in trades if t.setup == "A"]
    b_trades = [t for t in trades if t.setup == "B"]

    sample_a = sample_window(candles, a_trades[0].entry_ts) if a_trades else []
    sample_b = sample_window(candles, b_trades[0].entry_ts, before_min=240, after_min=300) if b_trades else []

    return {
        "meta": meta,
        "optimized_params": optimized_params or {"A": {}, "B": {}},
        "grid_search": grid_search or {"A": [], "B": [], "A_winrate_target": [0, 1], "B_winrate_target": [0, 1]},
        "summary": summary,
        "rejection": {
            "A": {"checks": a_checks, "rejected": a_rejected},
            "B": {"checks": b_checks, "rejected": b_rejected},
        },
        "trades": [trade_to_dict(t) for t in trades],
        "equity_curve": equity_curve,
        "price_overview": downsample_price(candles, step_minutes=max(1, len(candles) // 3000 or 1)),
        "sample_window_a": sample_a,
        "sample_window_b": sample_b,
        "sample_trade_a": trade_to_dict(a_trades[0]) if a_trades else None,
        "sample_trade_b": trade_to_dict(b_trades[0]) if b_trades else None,
    }
