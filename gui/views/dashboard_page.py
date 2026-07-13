"""Dashboard 페이지 — 실시간 상태 + 캔들차트 (읽기 전용).

이 페이지는 EventBus를 구독만 하고 컨트롤러/엔진을 직접 호출하지 않는다.

구독 토픽:
- "candle":         1m 확정봉 (텍스트 라벨 + 합성 모드 차트 집계 입력)
- "candle_tf":      라이브 모드의 타임프레임별(1m/5m/1h/1d) 네이티브 확정봉
- "candle_history": 라이브 시작 시 REST 백필 (타임프레임별 일괄 초기화)
- "status"/"position"/"summary": 기존과 동일
"""

from tkinter import ttk

from gui.widgets.status_widget import StatusWidget
from gui.widgets.position_widget import PositionWidget
from gui.widgets.pnl_widget import PnlWidget
from gui.widgets.candle_chart import CandleChart


class DashboardPage:
    def __init__(self, parent, bus, controller):
        self.frame = ttk.Frame(parent)

        top = ttk.Frame(self.frame)
        top.pack(fill="x", padx=10, pady=10)
        self.status = StatusWidget(top)
        self.status.pack(side="left")
        self.price_label = ttk.Label(top, text="가격: -", font=("Arial", 12, "bold"))
        self.price_label.pack(side="left", padx=20)
        self.box_label = ttk.Label(top, text="박스(4h): -")
        self.box_label.pack(side="left", padx=20)

        # ---- 실시간 캔들차트 ----
        self.chart = CandleChart(self.frame, height=300)
        self.chart.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        ttk.Label(self.frame, text="현재 포지션", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=10, pady=(5, 0))
        self.positions = PositionWidget(self.frame)
        self.positions.pack(fill="x", padx=10, pady=5)

        ttk.Label(self.frame, text="누적 성과 (Setup별)", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 0))
        self.pnl = PnlWidget(self.frame)
        self.pnl.pack(fill="x", padx=10, pady=5)

        bus.subscribe("status", self.status.update_status)
        bus.subscribe("candle", self._on_candle)
        bus.subscribe("candle_tf", self._on_candle_tf)
        bus.subscribe("candle_history", self._on_candle_history)
        bus.subscribe("position", self.positions.update_positions)
        bus.subscribe("summary", self.pnl.update_summary)

    def _on_candle(self, data):
        self.price_label.config(text=f"가격: {data['close']:.1f}")
        if data.get("box_low") is not None:
            self.box_label.config(text=f"박스(4h): {data['box_low']:.1f} ~ {data['box_high']:.1f}")
            self.chart.set_box(data["box_low"], data["box_high"])
        # 합성 모드: 1m -> 5m/1h/1d 집계는 차트가 내부에서 처리
        # (라이브 모드에서 네이티브 봉이 들어오는 타임프레임은 자동으로 집계 중단)
        if "ts" in data:
            self.chart.add_1m({
                "ts": data["ts"], "open": data.get("open", data["close"]),
                "high": data.get("high", data["close"]),
                "low": data.get("low", data["close"]),
                "close": data["close"], "volume": data.get("volume", 0.0),
                "closed": True,
            })

    def _on_candle_tf(self, data):
        self.chart.add_native(data["interval"], data["candle"])

    def _on_candle_history(self, data):
        self.chart.set_history(data["interval"], data["candles"])
