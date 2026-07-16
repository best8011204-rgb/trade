"""Setup C 기각조건 판정 (명세서 6장) — report.py(A/B)와 별도 모듈.

Setup C는 원장 구조(방향별 dict)와 판정 기준(BE-WR, 반감기 대비 보유시간,
VR게이트 기여도, Setup A/B와의 상관)이 A/B와 근본적으로 달라 report.py를
공유하지 않는다.
"""

import pandas as pd

MIN_SAMPLES = 30


def rejection_check_c(stats: dict) -> tuple[list, bool]:
    """방향(롱 또는 숏) 하나의 stats에 대해 6장 규칙 1·2번을 판정한다."""
    checks = []
    count = stats.get("count", 0)
    avg_bps = stats.get("avg_bps", 0)
    win_rate = stats.get("win_rate", 0)
    be_wr = stats.get("breakeven_win_rate")
    avg_hold = stats.get("avg_bars_held", 0)
    avg_hl = stats.get("avg_half_life_bars", 0)

    applicable = count >= MIN_SAMPLES
    wr_flat_or_below = be_wr is not None and win_rate <= be_wr
    if be_wr is not None:
        wr_value = f"승률 {win_rate*100:.1f}% / BE-WR {be_wr*100:.1f}%"
    else:
        wr_value = "BE-WR 계산불가(승/패 표본 편중)"
    checks.append({
        "rule": f"{MIN_SAMPLES}건 도달 시 실측 승률 ≤ 손익분기승률(BE-WR) 이고 기댓값 ≤ 0",
        "applicable": applicable,
        "value": f"{count}건 / {wr_value} / 평균 {avg_bps:.1f}bps",
        "failed": bool(applicable and wr_flat_or_below and avg_bps <= 0),
    })

    hold_ratio = (avg_hold / avg_hl) if avg_hl else None
    checks.append({
        "rule": "평균 보유시간 > 2.5×반감기 (OU 가정 위배)",
        "applicable": bool(applicable and avg_hl),
        "value": (f"{avg_hold:.1f}봉 / 반감기 {avg_hl:.1f}봉 (비율 {hold_ratio:.2f}x)"
                  if hold_ratio is not None else "반감기 데이터 없음"),
        "failed": bool(applicable and hold_ratio is not None and hold_ratio > 2.5),
    })

    rejected = any(c["failed"] for c in checks)
    return checks, rejected


def rejection_check_vr_gate(stats_on: dict, stats_off: dict) -> dict:
    """규칙3: VR 게이트 on/off 비교에서 게이트 기여(on 평균bps − off 평균bps)가
    0 이하면 기각(H2 반증)."""
    on_count, off_count = stats_on.get("count", 0), stats_off.get("count", 0)
    applicable = on_count >= MIN_SAMPLES and off_count >= MIN_SAMPLES
    on_bps = stats_on.get("avg_bps", 0.0)
    off_bps = stats_off.get("avg_bps", 0.0)
    contribution = on_bps - off_bps
    return {
        "rule": "VR 게이트 on/off 비교에서 게이트 기여(on 평균bps − off 평균bps) ≤ 0",
        "applicable": applicable,
        "value": (f"on {on_bps:+.1f}bps({on_count}건) / off {off_bps:+.1f}bps({off_count}건) "
                  f"/ 기여 {contribution:+.1f}bps"),
        "failed": bool(applicable and contribution <= 0),
    }


def correlation_with_ab(c_trades: list, ab_bps_by_day: pd.Series, threshold: float = 0.6) -> dict:
    """규칙4: Setup C를 일별 bps 합으로 리샘플링해 Setup A/B 일별 bps와 상관계수 계산.

    ab_bps_by_day: DatetimeIndex(일) -> 그날 A/B 트레이드 bps 합. 호출측(A/B 백테스트
    결과)에서 미리 만들어 넘긴다 — 이 모듈은 Setup C 트레이드만 안다.
    """
    if not c_trades:
        return {"rule": "Setup A/B와 트레이드 손익 상관 > 0.6", "applicable": False,
                "value": "Setup C 표본 없음", "failed": False}
    df = pd.DataFrame(c_trades)
    df["day"] = pd.to_datetime(df["exit_ts"]).dt.floor("D")
    c_by_day = df.groupby("day")["bps"].sum()
    aligned = pd.concat([c_by_day, ab_bps_by_day.rename("ab")], axis=1).fillna(0.0)
    aligned.columns = ["c", "ab"]
    applicable = len(aligned) >= 10  # 상관계수가 의미 있으려면 최소 열흘 이상 겹쳐야 함
    corr = float(aligned["c"].corr(aligned["ab"])) if applicable else None
    return {
        "rule": "Setup A/B와 트레이드 손익 상관 > 0.6",
        "applicable": applicable,
        "value": (f"상관계수 {corr:.2f} (겹치는 날 {len(aligned)}일)" if applicable
                  else f"겹치는 날 {len(aligned)}일 (10일 미만 — 판정 보류)"),
        "failed": bool(applicable and corr is not None and corr > threshold),
    }
