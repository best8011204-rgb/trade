"""현재 보유 포지션(engine.open_legs)을 표시하는 위젯."""

from tkinter import ttk

COLUMNS = ("setup", "side", "entry", "sl", "tp1", "tp2", "qty", "tag")
HEADERS = ("Setup", "Side", "Entry", "SL", "TP1", "TP2", "Qty", "Tag")


class PositionWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.tree = ttk.Treeview(self, columns=COLUMNS, show="headings", height=5)
        for col, label in zip(COLUMNS, HEADERS):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=80, anchor="center")
        self.tree.pack(fill="both", expand=True)

    def update_positions(self, data):
        self.tree.delete(*self.tree.get_children())
        for leg in data.get("open_legs", []):
            self.tree.insert("", "end", values=(
                leg["setup"], leg["side"], f"{leg['entry_price']:.1f}",
                f"{leg['sl']:.1f}", f"{leg['tp1']:.1f}", f"{leg['tp2']:.1f}",
                f"{leg['qty_fraction']:.2f}", leg["tag"],
            ))
