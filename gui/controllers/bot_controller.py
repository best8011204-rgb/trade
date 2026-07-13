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


class BotController:
    def __init__(self, bus):
        self.bus = bus
        self.runner = None
        self.a_params = CascadeAParams()
        self.b_params = CascadeBParams()

    @property
    def is_running(self):
        return self.runner is not None and self.runner.is_alive

    def set_strategy_params(self, a_params=None, b_params=None):
        if a_params is not None:
            self.a_params = a_params
        if b_params is not None:
            self.b_params = b_params

    def start(self, symbol="BTCUSDT", speed=200.0, days=14, mode="synthetic"):
        if self.is_running:
            return
        if mode == "live":
            self.runner = LiveEngineRunner(
                self.bus, symbol=symbol,
                a_params=self.a_params, b_params=self.b_params,
            )
        else:
            self.runner = EngineRunner(
                self.bus, symbol=symbol, a_params=self.a_params,
                b_params=self.b_params, speed=speed, days=days,
            )
        self.runner.start()

    def stop(self):
        if self.runner:
            self.runner.stop()
