"""세로 스크롤 가능한 컨테이너. Canvas + interior Frame + Scrollbar 패턴.

마우스 휠(Windows/Mac `<MouseWheel>`, Linux `<Button-4>`/`<Button-5>`)은
포인터가 이 위젯 위에 있을 때만 전역으로 바인딩했다가 벗어나면 해제한다
— 그래야 같은 화면의 다른 스크롤 가능한 위젯(Treeview 등)의 휠 동작을
가로채지 않는다.
"""

import tkinter as tk
from tkinter import ttk

from gui import theme


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, bg=None):
        super().__init__(parent, style="Panel.TFrame")
        bg = bg or theme.PANEL_BG

        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg)
        vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        self.interior = ttk.Frame(self.canvas, style="Panel.TFrame")
        self._window = self.canvas.create_window((0, 0), window=self.interior, anchor="nw")

        self.interior.bind("<Configure>", self._on_interior_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_interior_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self._window, width=event.width)

    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)

    def _unbind_wheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(1, "units")
        else:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
