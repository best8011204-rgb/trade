"""Setup별 누적 성과 표시 위젯. "summary" 토픽(engine.summary() 결과) 구독."""

from tkinter import ttk


class PnlWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.labels = {}
        for i, setup in enumerate(("A", "B")):
            box = ttk.LabelFrame(self, text=f"Setup {setup}")
            box.grid(row=0, column=i, padx=5, sticky="ew")
            self.grid_columnconfigure(i, weight=1)
            lbl = ttk.Label(box, text="거래 없음", justify="left")
            lbl.pack(anchor="w", padx=6, pady=4)
            self.labels[setup] = lbl

    def update_summary(self, data):
        for setup in ("A", "B"):
            stats = data.get(setup, {})
            lbl = self.labels[setup]
            count = stats.get("count", 0)
            if not count:
                lbl.config(text="거래 없음")
                continue
            lbl.config(text=(
                f"건수: {count}\n"
                f"승률: {stats.get('win_rate', 0) * 100:.1f}%\n"
                f"평균 bps: {stats.get('avg_bps', 0):.1f}\n"
                f"평균 R: {stats.get('avg_r', 0):.2f}\n"
                f"최대 연속손실: {stats.get('max_consec_losses', 0)}"
            ))
