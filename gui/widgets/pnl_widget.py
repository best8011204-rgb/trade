"""Setup별 누적 성과 표시 위젯 (A/B/C/합산). "summary" 토픽 구독.

A/B는 StrategyEngine.summary(), C는 LiveSetupCRunner.combined_summary()가
채워준다(C는 롱/숏 독립 원장이지만 이 표에는 합산 1행으로 보여준다 — 방향별
상세는 Log 탭 개별 트레이드로 확인). "합산" 행은 세 셋업을 표본수 가중평균
으로 화면(GUI) 쪽에서 재계산한다 — 각 셋업의 summary() 자체는 건드리지
않는다. 최대 연속손실은 서로 다른 셋업의 트레이드가 실제로 섞여 발생한
시간순서를 여기서는 알 수 없어 정확히 재계산할 수 없으므로, 셋 중 가장 큰
값을 보수적으로 보여준다.
"""

from tkinter import ttk

from gui import theme

COLUMNS = ("setup", "count", "win_rate", "avg_bps", "avg_r", "max_consec")
HEADERS = {
    "setup": "Setup", "count": "표본수", "win_rate": "승률",
    "avg_bps": "평균bps", "avg_r": "평균R", "max_consec": "최대연속손실",
}
WIDTHS = {"setup": 60, "count": 60, "win_rate": 65, "avg_bps": 75, "avg_r": 60, "max_consec": 90}
ROWS = ("A", "B", "C", "합산")


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
        c = data.get("C", {}) or {}
        self._update_row("A", a)
        self._update_row("B", b)
        self._update_row("C", c)
        self._update_row("합산", _combine(a, b, c))

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


def _combine(a, b, c):
    counts = {"a": a.get("count", 0), "b": b.get("count", 0), "c": c.get("count", 0)}
    count = sum(counts.values())
    if not count:
        return {}
    stats = {"a": a, "b": b, "c": c}
    win_rate = sum(stats[k].get("win_rate", 0) * counts[k] for k in counts) / count
    avg_bps = sum(stats[k].get("avg_bps", 0) * counts[k] for k in counts) / count
    avg_r = sum(stats[k].get("avg_r", 0) * counts[k] for k in counts) / count
    max_consec = max(stats[k].get("max_consec_losses", 0) for k in counts)
    return {
        "count": count, "win_rate": win_rate, "avg_bps": avg_bps,
        "avg_r": avg_r, "max_consec_losses": max_consec,
    }
