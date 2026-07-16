"""Dashboard 페이지 — 바이낸스 터미널 스타일 3컬럼 레이아웃 (읽기 전용).

이 페이지는 EventBus를 구독만 하고 컨트롤러/엔진을 직접 호출하지 않는다.
StrategyEngine/LiveEngineRunner/EventBus/live_feed 쪽 로직은 전혀 건드리지
않았다 — 여기 있는 변경은 전부 렌더링/레이아웃뿐이다.

레이아웃 (grid):
    row0  상단바(고정) — 연결상태 / 가격 / 박스(4h) / OI / 모드
    row1  본문(weight=1, 3컬럼) — 좌 300px 고정: 트리거 조건(스크롤) /
          중앙 weight=1: 차트 / 우 380px 고정: 포지션(위)·성과(아래)
    row2  하단 상태바(고정) — 상세 상태 메시지

구독 토픽 (기존과 동일, 데이터 흐름 변경 없음):
- "candle":         1m 확정봉 (가격/박스 갱신 + 합성 모드 차트 집계 입력)
- "candle_tf":      라이브 모드의 타임프레임별(1m/5m/1h/1d) 네이티브 봉
                    (진행 중인 봉 포함, 1m 수신 시마다 가격도 갱신)
- "candle_history": 라이브 시작 시 REST 백필
- "oi"/"oi_history": OI 포인트 / 히스토리
- "intent":         Setup A/B/C 대기 요약 텍스트 + 참고가
- "conditions":     T1~T4(A)/T1~T3(B)/G1~G4(C) 하위 조건 체크리스트
- "status"/"position"/"summary": 연결상태 / 보유 포지션 / 누적 성과
"""

from tkinter import ttk

from gui import theme
from gui.widgets.status_widget import StatusWidget
from gui.widgets.position_widget import PositionWidget
from gui.widgets.pnl_widget import PnlWidget
from gui.widgets.candle_chart import CandleChart
from gui.widgets.trigger_section import TriggerSection
from gui.widgets.scrollable_frame import ScrollableFrame

LEFT_WIDTH = 300
RIGHT_WIDTH = 380


class DashboardPage:
    def __init__(self, parent, bus, controller):
        self.frame = ttk.Frame(parent)
        self.frame.grid_rowconfigure(0, weight=0)
        self.frame.grid_rowconfigure(1, weight=1)
        self.frame.grid_rowconfigure(2, weight=0)
        self.frame.grid_columnconfigure(0, weight=1)

        self._last_price = None
        self._last_price_color = theme.TEXT_PRIMARY
        self._intent_a_text = "-"
        self._intent_b_text = "-"
        self._intent_c_text = "-"
        self._a_conditions = []
        self._b_conditions = []
        self._c_conditions = []

        self._build_topbar()
        self._build_body()
        self._build_bottombar()

        bus.subscribe("status", self._on_status)
        bus.subscribe("candle", self._on_candle)
        bus.subscribe("candle_tf", self._on_candle_tf)
        bus.subscribe("candle_history", self._on_candle_history)
        bus.subscribe("oi", self._on_oi)
        bus.subscribe("oi_history", self._on_oi_history)
        bus.subscribe("position", self._on_position)
        bus.subscribe("intent", self._on_intent)
        bus.subscribe("conditions", self._on_conditions)
        bus.subscribe("summary", self.pnl.update_summary)

    # ------------------------------------------------------------------
    # 상단바 — 바이낸스 티커 헤더처럼 라벨(작게, 위)/값(크게, 아래) 쌍
    # ------------------------------------------------------------------
    def _build_topbar(self):
        bar = ttk.Frame(self.frame, style="Panel.TFrame")
        bar.grid(row=0, column=0, sticky="ew")

        self.status = StatusWidget(bar)
        self.status.pack(side="left", padx=(12, 24), pady=10)

        self.price_value = self._stat_block(bar, "BTCUSDT")
        self.box_value = self._stat_block(bar, "박스(4h)")
        self.oi_value = self._stat_block(bar, "OI")
        self.mode_value = self._stat_block(bar, "모드")
        self.mode_value.config(text="섀도 · 실주문 없음", foreground=theme.TEXT_SECONDARY)

    def _stat_block(self, parent, label_text):
        box = ttk.Frame(parent, style="Panel.TFrame")
        box.pack(side="left", padx=(0, 28), pady=6)
        ttk.Label(box, text=label_text, style="Muted.TLabel").pack(anchor="w")
        value = ttk.Label(box, text="-", style="Value.TLabel")
        value.pack(anchor="w")
        return value

    def _set_price(self, price):
        color = self._last_price_color
        if self._last_price is not None:
            if price > self._last_price:
                color = theme.LONG_GREEN
            elif price < self._last_price:
                color = theme.SHORT_RED
        self._last_price = price
        self._last_price_color = color
        self.price_value.config(text=f"{price:,.1f}", foreground=color)

    # ------------------------------------------------------------------
    # 본문 — 좌(고정) / 중앙(신축) / 우(고정) 3컬럼
    # ------------------------------------------------------------------
    def _build_body(self):
        body = ttk.Frame(self.frame)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=0, minsize=LEFT_WIDTH)
        body.grid_columnconfigure(1, weight=1)
        body.grid_columnconfigure(2, weight=0, minsize=RIGHT_WIDTH)

        self._build_left(body)
        self._build_center(body)
        self._build_right(body)

    def _build_left(self, body):
        left = ttk.Frame(body, width=LEFT_WIDTH, height=400, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="nsew")
        # 내부 콘텐츠는 pack()으로 배치되므로 pack_propagate를 꺼야 폭이
        # 고정된다 (grid_propagate는 grid()로 배치된 자식에만 적용됨).
        left.pack_propagate(False)

        ttk.Label(left, text="트리거 조건", style="Heading.TLabel").pack(
            anchor="w", padx=8, pady=(8, 4))

        scroller = ScrollableFrame(left)
        scroller.pack(fill="both", expand=True)

        self.trigger_a = TriggerSection(scroller.interior, "Setup A", theme.LONG_GREEN, max_rows=7)
        self.trigger_a.pack(fill="x")
        self.trigger_b = TriggerSection(scroller.interior, "Setup B", theme.SHORT_RED, max_rows=4)
        self.trigger_b.pack(fill="x")
        self.trigger_c = TriggerSection(scroller.interior, "Setup C", theme.ACCENT_YELLOW, max_rows=6)
        self.trigger_c.pack(fill="x")

    def _build_center(self, body):
        center = ttk.Frame(body)
        center.grid(row=0, column=1, sticky="nsew")
        self.chart = CandleChart(center, height=400)
        self.chart.pack(fill="both", expand=True, padx=6, pady=6)

    def _build_right(self, body):
        right = ttk.Frame(body, width=RIGHT_WIDTH, height=400, style="Panel.TFrame")
        right.grid(row=0, column=2, sticky="nsew")
        right.pack_propagate(False)  # 좌패널과 동일한 이유 (내부는 pack() 배치)

        paned = ttk.PanedWindow(right, orient="vertical")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        pos_box = ttk.Frame(paned, style="Panel.TFrame")
        ttk.Label(pos_box, text="현재 포지션", style="Heading.TLabel").pack(
            anchor="w", padx=4, pady=(4, 2))
        self.positions = PositionWidget(pos_box)
        self.positions.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        paned.add(pos_box, weight=2)

        pnl_box = ttk.Frame(paned, style="Panel.TFrame")
        ttk.Label(pnl_box, text="누적 성과 (Setup별)", style="Heading.TLabel").pack(
            anchor="w", padx=4, pady=(4, 2))
        self.pnl = PnlWidget(pnl_box)
        self.pnl.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        paned.add(pnl_box, weight=3)

    # ------------------------------------------------------------------
    # 하단 상태바
    # ------------------------------------------------------------------
    def _build_bottombar(self):
        bar = ttk.Frame(self.frame, style="Panel.TFrame")
        bar.grid(row=2, column=0, sticky="ew")
        self.status_message = ttk.Label(bar, text="", style="Muted.TLabel")
        self.status_message.pack(anchor="w", padx=12, pady=4)

    # ------------------------------------------------------------------
    # 이벤트 핸들러
    # ------------------------------------------------------------------
    def _on_status(self, data):
        self.status.update_status(data)
        self.status_message.config(text=data.get("message", ""))

    def _on_candle(self, data):
        self._set_price(data["close"])
        if data.get("box_low") is not None:
            self.box_value.config(text=f"{data['box_low']:.1f} ~ {data['box_high']:.1f}")
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
        self._intent_a_text = data.get("a_text", "-")
        self._intent_b_text = data.get("b_text", "-")
        self._intent_c_text = data.get("c_text", "-")
        self.trigger_a.update(self._intent_a_text, self._a_conditions)
        self.trigger_b.update(self._intent_b_text, self._b_conditions)
        self.trigger_c.update(self._intent_c_text, self._c_conditions)
        self.chart.set_watch("a", data.get("a_level"))
        self.chart.set_watch("b", data.get("b_level"))

    def _on_conditions(self, data):
        self._a_conditions = data.get("a_conditions", [])
        self._b_conditions = data.get("b_conditions", [])
        self._c_conditions = data.get("c_conditions", [])
        self.trigger_a.update(self._intent_a_text, self._a_conditions)
        self.trigger_b.update(self._intent_b_text, self._b_conditions)
        self.trigger_c.update(self._intent_c_text, self._c_conditions)

    def _on_candle_tf(self, data):
        self.chart.add_native(data["interval"], data["candle"])
        if data["interval"] == "1m":
            self._set_price(data["candle"]["close"])

    def _on_candle_history(self, data):
        self.chart.set_history(data["interval"], data["candles"])

    def _on_oi(self, data):
        from gui.widgets.candle_chart import _fmt_oi
        self.oi_value.config(text=_fmt_oi(data["oi"]))
        self.chart.add_oi(data["ts"], data["oi"])

    def _on_oi_history(self, data):
        from gui.widgets.candle_chart import _fmt_oi
        points = data["points"]
        self.chart.set_oi_history(points)
        if points:
            self.oi_value.config(text=_fmt_oi(points[-1][1]))
