"""체결 완료된 트레이드 이력을 최신순으로 보여주는 테이블 위젯."""

from tkinter import ttk

COLUMNS = ("setup", "side", "entry", "exit", "reason", "bps", "r", "tag")
HEADERS = ("Setup", "Side", "Entry", "Exit", "Reason", "bps", "R", "Tag")


class TradesTable(ttk.Frame):
    def __init__(self, parent, max_rows=200):
        super().__init__(parent)
        self.max_rows = max_rows
        self.tree = ttk.Treeview(self, columns=COLUMNS, show="headings", height=10)
        for col, label in zip(COLUMNS, HEADERS):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=80, anchor="center")
        self.tree.pack(fill="both", expand=True)

    def add_trade(self, t):
        self.tree.insert("", 0, values=(
            t["setup"], t["side"], f"{t['entry_price']:.1f}", f"{t['exit_price']:.1f}",
            t["reason"], f"{t['bps']:.1f}", f"{t['r_multiple']:.2f}", t["tag"],
        ))
        children = self.tree.get_children()
        if len(children) > self.max_rows:
            self.tree.delete(children[-1])
