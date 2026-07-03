"""Dashboard 페이지 — 실시간 상태만 보여준다 (읽기 전용).

이 페이지는 EventBus를 구독만 하고 컨트롤러/엔진을 직접 호출하지 않는다.
"""

from tkinter import ttk

from gui.widgets.status_widget import StatusWidget
from gui.widgets.position_widget import PositionWidget
from gui.widgets.pnl_widget import PnlWidget


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

        ttk.Label(self.frame, text="현재 포지션", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 0))
        self.positions = PositionWidget(self.frame)
        self.positions.pack(fill="x", padx=10, pady=5)

        ttk.Label(self.frame, text="누적 성과 (Setup별)", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 0))
        self.pnl = PnlWidget(self.frame)
        self.pnl.pack(fill="x", padx=10, pady=5)

        bus.subscribe("status", self.status.update_status)
        bus.subscribe("candle", self._on_candle)
        bus.subscribe("position", self.positions.update_positions)
        bus.subscribe("summary", self.pnl.update_summary)

    def _on_candle(self, data):
        self.price_label.config(text=f"가격: {data['close']:.1f}")
        if data.get("box_low") is not None:
            self.box_label.config(text=f"박스(4h): {data['box_low']:.1f} ~ {data['box_high']:.1f}")
