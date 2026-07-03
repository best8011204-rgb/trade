"""봇/연결 상태를 색상 인디케이터 + 메시지로 표시하는 위젯."""

import tkinter as tk
from tkinter import ttk


class StatusWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.dot = tk.Label(self, text="●", fg="gray", font=("Arial", 14))
        self.dot.pack(side="left")
        self.text = ttk.Label(self, text="대기 중")
        self.text.pack(side="left", padx=4)

    def update_status(self, data):
        running = bool(data.get("running", False))
        self.dot.config(fg="green" if running else "gray")
        self.text.config(text=data.get("message", "대기 중"))
