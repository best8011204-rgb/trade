"""Logs 페이지 — 시그널/체결/상태 이벤트를 콘솔 로그 + 트레이드 테이블로 표시."""

import time
from tkinter import ttk, scrolledtext, WORD

from gui.widgets.trades_table import TradesTable


class LogPage:
    def __init__(self, parent, bus, controller):
        self.frame = ttk.Frame(parent)

        ttk.Label(self.frame, text="실시간 동작 로그", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 0))
        self.text = scrolledtext.ScrolledText(
            self.frame, wrap=WORD, state="disabled", bg="black", fg="lime", height=16,
        )
        self.text.pack(fill="both", expand=True, padx=10, pady=5)

        ttk.Label(self.frame, text="체결 이력", font=("Arial", 10, "bold")).pack(
            anchor="w", padx=10, pady=(10, 0))
        self.trades = TradesTable(self.frame)
        self.trades.pack(fill="both", expand=True, padx=10, pady=5)

        bus.subscribe("status", lambda d: self._write(f"[시스템] {d.get('message', '')}"))
        bus.subscribe("signal", self._on_signal)
        bus.subscribe("trade_closed", self._on_trade)

    def _on_signal(self, data):
        ts_str = time.strftime("%H:%M:%S", time.localtime(data["ts"]))
        self._write(f"[{ts_str}][Setup {data['setup']}] {data['msg']}")

    def _on_trade(self, t):
        ts_str = time.strftime("%H:%M:%S", time.localtime(t["exit_ts"]))
        self._write(
            f"[{ts_str}][체결] Setup {t['setup']} {t['side']} "
            f"{t['reason']} bps={t['bps']:.1f} R={t['r_multiple']:.2f} tag={t['tag']}"
        )
        self.trades.add_trade(t)

    def _write(self, line):
        self.text.config(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.config(state="disabled")
