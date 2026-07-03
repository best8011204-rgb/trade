"""Trading 페이지 — 실행 설정 및 Start/Stop.

버튼은 BotController만 호출한다. 엔진(StrategyEngine)을 직접 참조하지 않는다.
"""

from tkinter import ttk, messagebox


class TradingPage:
    def __init__(self, parent, bus, controller):
        self.frame = ttk.Frame(parent)
        self.controller = controller

        settings = ttk.LabelFrame(self.frame, text="시뮬레이션 설정")
        settings.pack(fill="x", padx=10, pady=10)

        ttk.Label(settings, text="Symbol:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.symbol_combo = ttk.Combobox(settings, values=["BTCUSDT"], state="readonly", width=15)
        self.symbol_combo.current(0)
        self.symbol_combo.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        ttk.Label(settings, text="배속 (x):").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.speed_entry = ttk.Entry(settings, width=15)
        self.speed_entry.insert(0, "200")
        self.speed_entry.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        ttk.Label(settings, text="합성 기간 (days):").grid(row=2, column=0, padx=5, pady=5, sticky="w")
        self.days_entry = ttk.Entry(settings, width=15)
        self.days_entry.insert(0, "14")
        self.days_entry.grid(row=2, column=1, padx=5, pady=5, sticky="w")

        note = ttk.Label(
            self.frame,
            text=(
                "현재 모드: 합성 데이터 섀도 시뮬레이션 (실주문 없음, API 키 불필요)\n"
                "실거래소 실시간 연동은 liquidation_strategy/live_feed.py 참고\n"
                "(웹소켓 아웃바운드가 열린 환경에서만 동작 — 이 GUI와 동일한 엔진/이벤트를 사용)"
            ),
            foreground="gray",
            justify="left",
        )
        note.pack(anchor="w", padx=10, pady=(0, 10))

        control = ttk.Frame(self.frame)
        control.pack(fill="x", padx=10, pady=10)

        self.btn_start = ttk.Button(control, text="Start", command=self._start)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(control, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=5)

        bus.subscribe("status", self._on_status)

    def _start(self):
        try:
            speed = float(self.speed_entry.get())
            days = int(self.days_entry.get())
        except ValueError:
            messagebox.showwarning("입력 오류", "배속/기간은 숫자로 입력해주세요.")
            return

        self.controller.start(symbol=self.symbol_combo.get(), speed=speed, days=days)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

    def _stop(self):
        self.controller.stop()
        self.btn_stop.config(state="disabled")

    def _on_status(self, data):
        if not data.get("running", False):
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
