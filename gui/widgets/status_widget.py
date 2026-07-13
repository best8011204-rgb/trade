"""러너 상태(running/connected/message) 표시 위젯."""

from tkinter import ttk

RUNNING_COLOR = "#0ecb81"
STOPPED_COLOR = "#f6465d"
CONNECTING_COLOR = "#f0b90b"


class StatusWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.dot = ttk.Label(self, text="●", foreground=STOPPED_COLOR, font=("Arial", 12))
        self.dot.pack(side="left")
        self.text = ttk.Label(self, text="정지됨")
        self.text.pack(side="left", padx=(4, 0))

    def update_status(self, data):
        running = data.get("running", False)
        connected = data.get("connected", False)
        message = data.get("message", "")
        if running and connected:
            color = RUNNING_COLOR
        elif running:
            color = CONNECTING_COLOR
        else:
            color = STOPPED_COLOR
        self.dot.config(foreground=color)
        self.text.config(text=message or ("실행 중" if running else "정지됨"))
