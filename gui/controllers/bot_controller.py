"""GUI 버튼이 엔진을 직접 호출하지 않도록 하는 컨트롤러.

Trading 페이지의 Start/Stop 버튼은 이 클래스의 메서드만 호출한다. 실제 엔진
구동은 러너(백그라운드 스레드)가 담당하고, 컨트롤러는 그 생명주기
(생성/시작/정지)와 다음 실행에 쓸 전략 파라미터, 실행 모드만 관리한다.

mode:
- "synthetic": EngineRunner   — 합성 데이터 시뮬레이션 (어디서나 동작)
- "live":      LiveEngineRunner — Binance 실시간 웹소켓 섀도 (실주문 없음,
               인터넷이 열린 환경 전용)
"""

from gui.engine_bridge import EngineRunner
from gui.live_engine_bridge import LiveEngineRunner
from liquidation_strategy.setup_a import CascadeAParams
from liquidation_strategy.setup_b import CascadeBParams
from liquidation_strategy.setup_c import ParamsC


class BotController:
    def __init__(self, bus):
        self.bus = bus
        self.runner = None
        self.a_params = CascadeAParams()
        self.b_params = CascadeBParams()
        self.c_params = ParamsC()

    @property
    def is_running(self):
        return self.runner is not None and self.runner.is_alive

    def set_strategy_params(self, a_params=None, b_params=None, c_params=None):
        if a_params is not None:
            self.a_params = a_params
        if b_params is not None:
            self.b_params = b_params
        if c_params is not None:
            self.c_params = c_params

    def stop_setup(self, setup: str):
        """Settings 페이지 "중지" 버튼. 실행 중인 러너가 있을 때만 의미가
        있다 — 해당 셋업의 신규 신호 감지를 멈추고, 진행 중이던 포지션은
        기록하지 않고 버린다."""
        if self.is_running:
            self.runner.stop_setup(setup)

    def restart_setup(self, setup: str, params):
        """Settings 페이지 "적용" — 실행 중인 러너가 있으면 해당 셋업을 새
        파라미터로 즉시 재시작한다(진행 중 포지션은 기록 없이 버림). 다음
        전체 Start에도 같은 파라미터가 쓰이도록 보관값도 함께 갱신한다."""
        if setup == "A":
            self.a_params = params
        elif setup == "B":
            self.b_params = params
        else:
            self.c_params = params
        if self.is_running:
            self.runner.restart_setup(setup, params)

    def start(self, symbol="BTCUSDT", speed=200.0, days=14, mode="synthetic"):
        if self.is_running:
            return
        if mode == "live":
            self.runner = LiveEngineRunner(
                self.bus, symbol=symbol,
                a_params=self.a_params, b_params=self.b_params, c_params=self.c_params,
            )
        else:
            self.runner = EngineRunner(
                self.bus, symbol=symbol, a_params=self.a_params,
                b_params=self.b_params, c_params=self.c_params, speed=speed, days=days,
            )
        self.runner.start()

    def stop(self):
        if self.runner:
            self.runner.stop()
