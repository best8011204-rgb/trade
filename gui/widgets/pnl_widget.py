"""engine.summary()를 Setup별 누적 성과 지표로 표시하는 위젯."""

from tkinter import ttk


class PnlWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.labels = {}
        for i, setup in enumerate(("A", "B")):
            frame = ttk.LabelFrame(self, text=f"Setup {setup}")
            frame.grid(row=0, column=i, padx=5, pady=5, sticky="nsew")
            lbl = ttk.Label(frame, text="거래 없음", justify="left")
            lbl.pack(padx=8, pady=8, anchor="w")
            self.labels[setup] = lbl
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

    def update_summary(self, summary):
        summary = summary or {}
        for setup, lbl in self.labels.items():
            s = summary.get(setup, {})
            count = s.get("count", 0)
            if not count:
                lbl.config(text="거래 없음")
                continue
            lbl.config(text=(
                f"거래수: {count}\n"
                f"승률: {s['win_rate'] * 100:.1f}%\n"
                f"평균 bps: {s['avg_bps']:.2f}\n"
                f"평균 R: {s['avg_r']:.2f}\n"
                f"최대 연속손실: {s['max_consec_losses']}"
            ))
