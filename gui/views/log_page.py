"""Log 페이지 — 시그널/체결/설정변경 이벤트 원시 로그 (읽기 전용).

"signal"/"trade_closed"/"status"를 구독해 최근 이벤트를 텍스트로 누적
표시한다. "config"는 Settings 페이지의 "적용"/"중지" 버튼이나 텔레그램
/set·/pause·/resume·/close all 처럼, 설정/전략 파라미터가 실제로 바뀌어
반영될 때마다 발행되는 토픽이다 — 어떤 경로로 바뀌었든 여기 한 곳에서
전부 확인할 수 있다.
"""

import time
import tkinter as tk
from tkinter import ttk

from gui import theme

MAX_LINES = 1000


class LogPage:
    def __init__(self, parent, bus, controller):
        self.frame = ttk.Frame(parent)

        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Button(toolbar, text="지우기", command=self._clear).pack(side="right")

        text_frame = ttk.Frame(self.frame)
        text_frame.pack(fill="both", expand=True, padx=10, pady=10)
        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")
        self.text = tk.Text(text_frame, height=20, state="disabled", wrap="word",
                            yscrollcommand=scrollbar.set,
                            bg=theme.PANEL_BG, fg=theme.TEXT_PRIMARY,
                            insertbackground=theme.TEXT_PRIMARY,
                            relief="flat", borderwidth=0)
        self.text.pack(fill="both", expand=True)
        scrollbar.config(command=self.text.yview)

        bus.subscribe("signal", self._on_signal)
        bus.subscribe("trade_closed", self._on_trade_closed)
        bus.subscribe("status", self._on_status)
        bus.subscribe("config", self._on_config)

    def _append(self, line):
        self.text.config(state="normal")
        self.text.insert("end", line + "\n")
        n_lines = int(self.text.index("end-1c").split(".")[0])
        if n_lines > MAX_LINES:
            self.text.delete("1.0", f"{n_lines - MAX_LINES}.0")
        self.text.see("end")
        self.text.config(state="disabled")

    def _clear(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.config(state="disabled")

    def _on_signal(self, data):
        ts = data.get("ts")
        t = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
        self._append(f"[{t}] [{data.get('setup', '?')}] {data.get('msg', '')}")

    def _on_trade_closed(self, data):
        t = time.strftime("%H:%M:%S", time.localtime(data.get("exit_ts", 0)))
        self._append(
            f"[{t}] 체결 종료 Setup={data.get('setup')} {data.get('side')} "
            f"reason={data.get('reason')} bps={data.get('bps', 0):.1f} "
            f"R={data.get('r_multiple', 0):.2f} tag={data.get('tag')}"
        )

    def _on_status(self, data):
        self._append(f"[status] {data.get('message', '')}")

    def _on_config(self, data):
        ts = data.get("ts")
        t = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
        self._append(f"[{t}] {data.get('msg', '')}")
