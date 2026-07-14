"""현재 보유 중인 포지션(트랜치) 표시 위젯. "position" 토픽 구독 전용."""

from tkinter import ttk

COLUMNS = ("setup", "side", "entry_price", "qty_fraction", "sl", "tp1", "tp2", "tp1_hit", "tag")
HEADERS = {
    "setup": "Setup", "side": "Side", "entry_price": "진입가", "qty_fraction": "비율",
    "sl": "SL", "tp1": "TP1", "tp2": "TP2", "tp1_hit": "TP1 도달", "tag": "Tag",
}


class PositionWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.tree = ttk.Treeview(self, columns=COLUMNS, show="headings", height=5)
        for col in COLUMNS:
            self.tree.heading(col, text=HEADERS[col])
            self.tree.column(col, width=90, anchor="center")
        self.tree.pack(fill="x", expand=True)

    def update_positions(self, data):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for leg in data.get("open_legs", []):
            self.tree.insert("", "end", values=(
                leg.get("setup", ""), leg.get("side", ""),
                f"{leg.get('entry_price', 0):.1f}", f"{leg.get('qty_fraction', 0):.2f}",
                f"{leg.get('sl', 0):.1f}", f"{leg.get('tp1', 0):.1f}", f"{leg.get('tp2', 0):.1f}",
                "Y" if leg.get("tp1_hit") else "N", leg.get("tag", ""),
            ))
