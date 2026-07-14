"""Trading 페이지 — 실행 모드/설정 및 Start/Stop.

버튼은 BotController만 호출한다. 엔진(StrategyEngine)을 직접 참조하지 않는다.

실행 모드:
- 합성 데이터 시뮬레이션: 어디서나 동작 (네트워크 불필요)
- 실거래소 실시간 (섀도): Binance 웹소켓/REST 아웃바운드가 열린 환경 전용.
  실주문은 내지 않는다 — 트리거/가상 체결을 계산·기록만 한다.
"""

import tkinter as tk
from tkinter import ttk, messagebox


class TradingPage:
    def __init__(self, parent, bus, controller):
        self.frame = ttk.Frame(parent)
        self.controller = controller

        # ---- 실행 모드 ----
        mode_frame = ttk.LabelFrame(self.frame, text="실행 모드")
        mode_frame.pack(fill="x", padx=10, pady=(10, 0))
        self.mode_var = tk.StringVar(value="synthetic")
        ttk.Radiobutton(mode_frame, text="합성 데이터 시뮬레이션 (오프라인 가능)",
                        value="synthetic", variable=self.mode_var,
                        command=self._on_mode_change).pack(anchor="w", padx=8, pady=2)
        ttk.Radiobutton(mode_frame, text="실거래소 실시간 — Binance 섀도 (실주문 없음)",
                        value="live", variable=self.mode_var,
                        command=self._on_mode_change).pack(anchor="w", padx=8, pady=2)

        # ---- 설정 ----
        settings = ttk.LabelFrame(self.frame, text="설정")
        settings.pack(fill="x", padx=10, pady=10)
        ttk.Label(settings, text="Symbol:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.symbol_combo = ttk.Combobox(settings, values=["BTCUSDT"], state="readonly", width=15)
        self.symbol_combo.current(0)
        self.symbol_combo.grid(row=0, column=1, padx=5, pady=5, sticky="w")

        self.speed_label = ttk.Label(settings, text="배속 (x):")
        self.speed_label.grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.speed_entry = ttk.Entry(settings, width=15)
        self.speed_entry.insert(0, "200")
        self.speed_entry.grid(row=1, column=1, padx=5, pady=5, sticky="w")

        self.days_label = ttk.Label(settings, text="합성 기간 (days):")
        self.days_label.grid(row=2, column=0, padx=5, pady=5, sticky="w")
        self.days_entry = ttk.Entry(settings, width=15)
        self.days_entry.insert(0, "14")
        self.days_entry.grid(row=2, column=1, padx=5, pady=5, sticky="w")

        self.note = ttk.Label(self.frame, text="", foreground="gray", justify="left")
        self.note.pack(anchor="w", padx=10, pady=(0, 10))

        control = ttk.Frame(self.frame)
        control.pack(fill="x", padx=10, pady=10)
        self.btn_start = ttk.Button(control, text="Start", command=self._start)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(control, text="Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=5)

        bus.subscribe("status", self._on_status)
        self._on_mode_change()

    def _on_mode_change(self):
        live = self.mode_var.get() == "live"
        state = "disabled" if live else "normal"
        self.speed_entry.config(state=state)
        self.days_entry.config(state=state)
        if live:
            self.note.config(text=(
                "실거래소 실시간 (섀도): kline 1m/5m/1h/1d + forceOrder + aggTrade 웹소켓,\n"
                "OI 5분 REST 폴링. 실주문 없음 (API 키 불필요, 공개 데이터만 사용).\n"
                "인터넷이 열린 환경(로컬 PC/VPS)에서만 연결된다."
            ))
        else:
            self.note.config(text=(
                "합성 데이터 섀도 시뮬레이션 (실주문 없음, API 키 불필요).\n"
                "차트의 5m/1h/1d는 1m 봉을 로컬에서 집계한 것이다."
            ))

    def _start(self):
        mode = self.mode_var.get()
        speed, days = 0.0, 0
        if mode == "synthetic":
            try:
                speed = float(self.speed_entry.get())
                days = int(self.days_entry.get())
            except ValueError:
                messagebox.showwarning("입력 오류", "배속/기간은 숫자로 입력해주세요.")
                return
        self.controller.start(symbol=self.symbol_combo.get(), speed=speed, days=days, mode=mode)
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

    def start_live(self):
        """프로그램 시작 시 자동으로 Live 모드를 켤 때 쓰는 진입점."""
        self.mode_var.set("live")
        self._on_mode_change()
        self._start()

    def _stop(self):
        self.controller.stop()
        self.btn_stop.config(state="disabled")

    def _on_status(self, data):
        if not data.get("running", False):
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
