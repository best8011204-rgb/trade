"""전략 파라미터 폼을 dataclass로부터 자동 생성/적용하기 위한 헬퍼.

Setup A/B의 파라미터(CascadeAParams/CascadeBParams)는 이미 dataclass이므로,
필드 목록을 읽어 GUI 입력폼을 만들고 사용자가 입력한 값으로 새 dataclass
인스턴스를 만들어 되돌려주기만 한다. dataclass에 필드가 추가/삭제되어도 이
모듈과 StrategyPage 코드는 수정할 필요가 없다.
"""

import dataclasses

from liquidation_strategy.setup_a import CascadeAParams
from liquidation_strategy.setup_b import CascadeBParams

STRATEGY_PARAM_CLASSES = {
    "A": CascadeAParams,
    "B": CascadeBParams,
}


def default_instance(setup: str):
    return STRATEGY_PARAM_CLASSES[setup]()


def build_instance(setup: str, values: dict):
    """values: {field_name: str 입력값} -> 새 dataclass 인스턴스."""
    cls = STRATEGY_PARAM_CLASSES[setup]
    defaults = cls()
    kwargs = {}
    for f in dataclasses.fields(cls):
        raw = values.get(f.name)
        if raw is None or raw == "":
            continue
        kwargs[f.name] = _coerce(raw, getattr(defaults, f.name))
    return cls(**kwargs)


def _coerce(raw, default):
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
