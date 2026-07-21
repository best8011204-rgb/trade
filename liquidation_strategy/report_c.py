"""Setup C(Price x OI) 기각조건 판정 — report.py(A/B)와 별도 모듈.

OU 전용이던 half-life 기반 규칙(2번)은 제거한다 — 새 Setup C1/C2엔 OU 가정이
없다. 대신 "OI 게이트가 실제로 기여하는가"를 OI-필터 on/off 어블레이션으로
검증한다(옛 rejection_check_vr_gate와 동일한 패턴 — VR게이트 대신 OI게이트).
"""

import pandas as pd

MIN_SAMPLES = 30


def rejection_check_c(stats: dict) -> tuple[list, bool]:
    """count>=30 도달 시 실측 승률 <= 손익분기승률(BE-WR) 이고 기댓값 <= 0이면 기각."""
    checks = []
    count = stats.get("count", 0)
    avg_bps = stats.get("avg_bps", 0)
    win_rate = stats.get("win_rate", 0)
    be_wr = stats.get("breakeven_win_rate")

    applicable = count >= MIN_SAMPLES
    wr_flat_or_below = be_wr is not None and win_rate <= be_wr
    wr_value = (f"승률 {win_rate*100:.1f}% / BE-WR {be_wr*100:.1f}%"
                if be_wr is not None else "BE-WR 계산불가(승/패 표본 편중)")
    checks.append({
        "rule": f"{MIN_SAMPLES}건 도달 시 실측 승률 <= 손익분기승률(BE-WR) 이고 기댓값 <= 0",
        "applicable": applicable,
        "value": f"{count}건 / {wr_value} / 평균 {avg_bps:.1f}bps",
        "failed": bool(applicable and wr_flat_or_below and avg_bps <= 0),
    })
    rejected = any(c["failed"] for c in checks)
    return checks, rejected


def rejection_check_oi_gate_contribution(stats_with_oi: dict, stats_without_oi: dict) -> dict:
    """OI 게이트 on(정상 실행) vs off(가격/거래량 조건만, OI 게이트 무시하고
    돌린 어블레이션) 비교에서, on 쪽 평균bps 기여(on-off)가 0 이하면 기각.
    호출측(백테스트 스크립트)이 두 실행 결과의 stats를 미리 만들어 넘긴다."""
    on_count, off_count = stats_with_oi.get("count", 0), stats_without_oi.get("count", 0)
    applicable = on_count >= MIN_SAMPLES and off_count >= MIN_SAMPLES
    on_bps = stats_with_oi.get("avg_bps", 0.0)
    off_bps = stats_without_oi.get("avg_bps", 0.0)
    contribution = on_bps - off_bps
    return {
        "rule": "OI 게이트 on/off 비교에서 게이트 기여(on 평균bps - off 평균bps) <= 0",
        "applicable": applicable,
        "value": (f"on {on_bps:+.1f}bps({on_count}건) / off {off_bps:+.1f}bps({off_count}건) "
                  f"/ 기여 {contribution:+.1f}bps"),
        "failed": bool(applicable and contribution <= 0),
    }


def correlation_with_ab(c_trades: list, ab_bps_by_day: pd.Series, threshold: float = 0.6) -> dict:
    """Setup C(C1+C2 합산 권장)를 일별 bps 합으로 리샘플링해 Setup A/B 일별 bps와 상관계수 계산."""
    if not c_trades:
        return {"rule": "Setup A/B와 트레이드 손익 상관 > 0.6", "applicable": False,
                "value": "Setup C 표본 없음", "failed": False}
    df = pd.DataFrame(c_trades)
    df["day"] = pd.to_datetime(df["exit_ts"]).dt.floor("D")
    c_by_day = df.groupby("day")["bps"].sum()
    aligned = pd.concat([c_by_day, ab_bps_by_day.rename("ab")], axis=1).fillna(0.0)
    aligned.columns = ["c", "ab"]
    applicable = len(aligned) >= 10
    corr = float(aligned["c"].corr(aligned["ab"])) if applicable else None
    return {
        "rule": "Setup A/B와 트레이드 손익 상관 > 0.6",
        "applicable": applicable,
        "value": (f"상관계수 {corr:.2f} (겹치는 날 {len(aligned)}일)" if applicable
                  else f"겹치는 날 {len(aligned)}일 (10일 미만 — 판정 보류)"),
        "failed": bool(applicable and corr is not None and corr > threshold),
    }
