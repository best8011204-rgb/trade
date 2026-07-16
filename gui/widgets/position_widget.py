"""현재 보유 중인 포지션(트랜치) 표시 위젯. "position" 토픽 구독 전용.

우패널 고정 폭(380px)보다 컬럼 전체 폭이 넓을 수 있어 가로+세로 스크롤바를
모두 단다 — 각 컬럼에 stretch=False를 줘서 Treeview가 폭에 맞춰 컬럼을
욱여넣지 않고 실제로 가로 스크롤이 일어나게 한다.
"""

from tkinter import ttk

from gui import theme

COLUMNS = ("setup", "side", "entry_price", "qty_fraction", "sl", "tp1", "tp2", "tp1_hit", "tag")
HEADERS = {
    "setup": "Setup", "side": "Side", "entry_price": "진입가", "qty_fraction": "비율",
    "sl": "SL", "tp1": "TP1", "tp2": "TP2", "tp1_hit": "TP1도달", "tag": "Tag",
}
WIDTHS = {
    "setup": 55, "side": 60, "entry_price": 85, "qty_fraction": 55,
    "sl": 80, "tp1": 80, "tp2": 80, "tp1_hit": 65, "tag": 70,
}


class PositionWidget(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="Panel.TFrame")

        vbar = ttk.Scrollbar(self, orient="vertical")
        hbar = ttk.Scrollbar(self, orient="horizontal")
        self.tree = ttk.Treeview(
            self, columns=COLUMNS, show="headings", height=6,
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

        self.tree.tag_configure("long", foreground=theme.LONG_GREEN)
        self.tree.tag_configure("short", foreground=theme.SHORT_RED)
        self.tree.tag_configure("empty", foreground=theme.TEXT_MUTED)

    def update_positions(self, data):
        for row in self.tree.get_children():
            self.tree.delete(row)
        legs = data.get("open_legs", [])
        if not legs:
            self.tree.insert("", "end", tags=("empty",), values=(
                "포지션 없음 — 섀도 관찰 중", "", "", "", "", "", "", "", "",
            ))
            return
        for leg in legs:
            side = leg.get("side", "")
            tag = "long" if side == "LONG" else "short"
            self.tree.insert("", "end", tags=(tag,), values=(
                leg.get("setup", ""), side,
                f"{leg.get('entry_price', 0):.1f}", f"{leg.get('qty_fraction', 0):.2f}",
                f"{leg.get('sl', 0):.1f}", f"{leg.get('tp1', 0):.1f}", f"{leg.get('tp2', 0):.1f}",
                "Y" if leg.get("tp1_hit") else "N", leg.get("tag", ""),
            ))
