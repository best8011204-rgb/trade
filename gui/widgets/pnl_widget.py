"""Setup별 누적 성과 표시 위젯 (A/B/합산). "summary" 토픽(engine.summary()) 구독.

합산 행은 A/B 두 집계를 표본수 가중평균으로 화면(GUI) 쪽에서 재계산한다 —
StrategyEngine.summary()는 Setup별 통계만 주고 그 로직은 건드리지 않으므로,
여기서 표시용으로만 합친다. 최대 연속손실은 두 셋업의 트레이드가 실제로
섞여 발생한 시간순서를 여기서는 알 수 없어 정확히 재계산할 수 없으므로,
두 값 중 더 큰 쪽을 보수적으로 보여준다.
"""

from tkinter import ttk

from gui import theme

COLUMNS = ("setup", "count", "win_rate", "avg_bps", "avg_r", "max_consec")
HEADERS = {
    "setup": "Setup", "count": "표본수", "win_rate": "승률",
    "avg_bps": "평균bps", "avg_r": "평균R", "max_consec": "최대연속손실",
}
WIDTHS = {"setup": 60, "count": 60, "win_rate": 65, "avg_bps": 75, "avg_r": 60, "max_consec": 90}
ROWS = ("A", "B", "합산")


class PnlWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="Panel.TFrame")

        vbar = ttk.Scrollbar(self, orient="vertical")
        hbar = ttk.Scrollbar(self, orient="horizontal")
        self.tree = ttk.Treeview(
            self, columns=COLUMNS, show="headings", height=4,
            yscrollcommand=vbar.set, xscrollcommand=hbar.set,
        )
        vbar.config(command=self.tree.yview)
        hbar.config(command=self.tree.xview)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        for col in COLUMNS:
            self.tree.heading(col, text=HEADERS[col])
            self.tree.column(col, width=WIDTHS[col], minwidth=WIDTHS[col],
                              anchor="center", stretch=False)

        self.tree.tag_configure("pos", foreground=theme.LONG_GREEN)
        self.tree.tag_configure("neg", foreground=theme.SHORT_RED)
        self.tree.tag_configure("neutral", foreground=theme.TEXT_MUTED)

        for setup in ROWS:
            self.tree.insert("", "end", iid=setup, tags=("neutral",),
                              values=(setup, 0, "-", "-", "-", "-"))

    def update_summary(self, data):
        a = data.get("A", {}) or {}
        b = data.get("B", {}) or {}
        self._update_row("A", a)
        self._update_row("B", b)
        self._update_row("합산", _combine(a, b))

    def _update_row(self, iid, stats):
        count = stats.get("count", 0)
        if not count:
            self.tree.item(iid, tags=("neutral",), values=(iid, 0, "-", "-", "-", "-"))
            return
        avg_bps = stats.get("avg_bps", 0.0)
        tag = "pos" if avg_bps > 0 else ("neg" if avg_bps < 0 else "neutral")
        self.tree.item(iid, tags=(tag,), values=(
            iid, count,
            f"{stats.get('win_rate', 0) * 100:.1f}%",
            f"{avg_bps:+.1f}",
            f"{stats.get('avg_r', 0):+.2f}",
            stats.get("max_consec_losses", "-"),
        ))


def _combine(a, b):
    ca, cb = a.get("count", 0), b.get("count", 0)
    count = ca + cb
    if not count:
        return {}
    win_rate = (a.get("win_rate", 0) * ca + b.get("win_rate", 0) * cb) / count
    avg_bps = (a.get("avg_bps", 0) * ca + b.get("avg_bps", 0) * cb) / count
    avg_r = (a.get("avg_r", 0) * ca + b.get("avg_r", 0) * cb) / count
    max_consec = max(a.get("max_consec_losses", 0), b.get("max_consec_losses", 0))
    return {
        "count": count, "win_rate": win_rate, "avg_bps": avg_bps,
        "avg_r": avg_r, "max_consec_losses": max_consec,
    }
