"""Dashboard 페이지 — 실시간 상태 + 캔들차트 (읽기 전용).

이 페이지는 EventBus를 구독만 하고 컨트롤러/엔진을 직접 호출하지 않는다.

구독 토픽:
- "candle":         1m 확정봉 (박스 라벨 + 합성 모드 차트 집계 입력, 전략 엔진과 동일 주기)
- "candle_tf":      라이브 모드의 타임프레임별(1m/5m/1h/1d) 네이티브 봉 —
                    진행 중인 봉도 포함되어 바이낸스 갱신 주기(초 단위)로
                    들어온다. 1m 수신 시마다 가격 라벨도 함께 갱신한다.
- "candle_history": 라이브 시작 시 REST 백필 (타임프레임별 일괄 초기화)
- "oi":             OI 포인트 (라이브 5분 폴링 / 합성 oi_points) -> 라벨+서브차트
- "oi_history":     라이브 시작 시 OI 히스토리 REST 백필
- "intent":         Setup A/B가 현재 무엇을 노리고 있는지 텍스트 설명 + 참고가
                    (a_text/a_level/b_text/b_level) -> 상태 라벨 + 차트 점선
- "conditions":     T1~T4(A)/T1~T3(B) 하위 조건별 실시간 충족 여부
                    (a_conditions/b_conditions) -> 체크리스트. 라이브 모드는
                    1초마다, 합성 모드는 매 캔들마다 갱신된다.
- "status"/"position"/"summary": 기존과 동일
"""

from tkinter import ttk

from gui.widgets.status_widget import StatusWidget
from gui.widgets.position_widget import PositionWidget
from gui.widgets.pnl_widget import PnlWidget
from gui.widgets.candle_chart import CandleChart
from gui.widgets.condition_list import ConditionList


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
        self.oi_label = ttk.Label(top, text="OI: -", foreground="#b8860b")
        self.oi_label.pack(side="left", padx=20)

        # ---- Setup A/B가 지금 노리고 있는 상황 (텍스트) ----
        intent = ttk.Frame(self.frame)
        intent.pack(fill="x", padx=10, pady=(0, 5))
        self.intent_a = ttk.Label(intent, text="Setup A: -", foreground="#26a69a",
                                   wraplength=1000, justify="left")
        self.intent_a.pack(anchor="w")
        self.intent_b = ttk.Label(intent, text="Setup B: -", foreground="#ec407a",
                                   wraplength=1000, justify="left")
        self.intent_b.pack(anchor="w")

        # ---- Setup A/B 트리거 하위조건 실시간 체크리스트 ----
        cond_frame = ttk.Frame(self.frame)
        cond_frame.pack(fill="x", padx=10, pady=(0, 5))
        self.cond_a = ConditionList(cond_frame, max_rows=7)
        self.cond_a.pack(side="left", padx=(0, 30), anchor="n")
        self.cond_b = ConditionList(cond_frame, max_rows=4)
        self.cond_b.pack(side="left", anchor="n")

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
        bus.subscribe("oi", self._on_oi)
        bus.subscribe("oi_history", self._on_oi_history)
        bus.subscribe("position", self._on_position)
        bus.subscribe("intent", self._on_intent)
        bus.subscribe("conditions", self._on_conditions)
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

    def _on_position(self, data):
        self.positions.update_positions(data)
        self.chart.set_legs(data.get("open_legs", []))

    def _on_intent(self, data):
        self.intent_a.config(text=f"Setup A: {data.get('a_text', '-')}")
        self.intent_b.config(text=f"Setup B: {data.get('b_text', '-')}")
        self.chart.set_watch("a", data.get("a_level"))
        self.chart.set_watch("b", data.get("b_level"))

    def _on_conditions(self, data):
        self.cond_a.update_conditions("Setup A", data.get("a_conditions", []))
        self.cond_b.update_conditions("Setup B", data.get("b_conditions", []))

    def _on_candle_tf(self, data):
        self.chart.add_native(data["interval"], data["candle"])
        if data["interval"] == "1m":
            c = data["candle"]
            self.price_label.config(text=f"가격: {c['close']:.1f}")

    def _on_candle_history(self, data):
        self.chart.set_history(data["interval"], data["candles"])

    def _on_oi(self, data):
        from gui.widgets.candle_chart import _fmt_oi
        self.oi_label.config(text=f"OI: {_fmt_oi(data['oi'])}")
        self.chart.add_oi(data["ts"], data["oi"])

    def _on_oi_history(self, data):
        from gui.widgets.candle_chart import _fmt_oi
        points = data["points"]
        self.chart.set_oi_history(points)
        if points:
            self.oi_label.config(text=f"OI: {_fmt_oi(points[-1][1])}")
