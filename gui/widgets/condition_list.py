"""전략 트리거 하위조건 실시간 체크리스트 위젯.

Setup.conditions(now_ts)가 반환하는 고정 길이 리스트를 받아 각 조건을
충족(초록 ✓)/미충족(회색 ✗)으로 표시하고, 헤더에 "몇 개 중 몇 개 충족"을
보여준다. 행 개수가 고정이므로 위젯을 매번 새로 만들지 않고 미리 만든
라벨의 텍스트/색만 갱신한다 (깜빡임 없음).
"""

from tkinter import ttk

MET_COLOR = "#0ecb81"
UNMET_COLOR = "#848e9c"
ALL_MET_COLOR = "#0ecb81"
HEADER_COLOR = "#e8e8e8"


class ConditionList(ttk.Frame):
    def __init__(self, parent, max_rows=8):
        super().__init__(parent)
        self.header = ttk.Label(self, text="-", font=("Arial", 9, "bold"))
        self.header.pack(anchor="w")
        self._rows = []
        for _ in range(max_rows):
            lbl = ttk.Label(self, text="", font=("Arial", 8))
            lbl.pack(anchor="w", padx=(10, 0))
            self._rows.append(lbl)

    def update_conditions(self, title: str, conditions: list):
        total = len(conditions)
        met = sum(1 for c in conditions if c["met"])
        header_color = ALL_MET_COLOR if total and met == total else HEADER_COLOR
        self.header.config(text=f"{title} 충족조건: {met}/{total}", foreground=header_color)
        for i, lbl in enumerate(self._rows):
            if i < len(conditions):
                c = conditions[i]
                mark = "✓" if c["met"] else "✗"
                color = MET_COLOR if c["met"] else UNMET_COLOR
                lbl.config(text=f"{mark} {c['label']} — {c['detail']}", foreground=color)
            else:
                lbl.config(text="")
