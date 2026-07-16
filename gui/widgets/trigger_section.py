"""좌패널의 Setup A/B "트리거 조건" 섹션 위젯.

헤더(색 점 + 이름 + 충족 n/m 배지) + 대기 요약 1줄(describe()) + 조건
리스트(고정 행수, conditions()의 ✓/✗ + 텍스트 + 현재값)를 하나로 묶는다.
Setup.conditions(now_ts)/Setup.describe()의 반환값을 그대로 받아 그릴 뿐,
전략 쪽 코드는 전혀 건드리지 않는다.

행 개수가 고정이므로 위젯을 매번 새로 만들지 않고 미리 만든 라벨의
텍스트/색만 갱신한다 (깜빡임 없음).
"""

import tkinter as tk
from tkinter import ttk

from gui import theme

MET_COLOR = theme.LONG_GREEN
UNMET_COLOR = theme.TEXT_MUTED
BLOCK_COLOR = theme.SHORT_RED
# 이 키가 미충족이면 '아직 안 됨'이 아니라 '차단됨'이라 빨강으로 강조한다
# (예: Setup A의 T4 매크로 블랙아웃 — met=False가 "진입 차단 중"을 의미).
BLOCKING_KEYS = {"t4_macro"}

ROW_WRAP_PX = 260


class TriggerSection(ttk.Frame):
    def __init__(self, parent, name: str, dot_color: str, max_rows: int):
        super().__init__(parent, style="Panel.TFrame")

        header = ttk.Frame(self, style="Panel.TFrame")
        header.pack(fill="x", padx=8, pady=(8, 2))
        self._dot = tk.Label(header, text="●", fg=dot_color, bg=theme.PANEL_BG,
                              font=theme.FONTS["body"], bd=0)
        self._dot.pack(side="left")
        ttk.Label(header, text=name, style="Heading.TLabel").pack(side="left", padx=(4, 8))
        self._badge = tk.Label(header, text="0/0", fg=theme.PAGE_BG, bg=UNMET_COLOR,
                                font=theme.FONTS["small_bold"], padx=6, pady=1, bd=0)
        self._badge.pack(side="left")

        self._intent = ttk.Label(self, text="-", style="Muted.TLabel",
                                  wraplength=ROW_WRAP_PX, justify="left")
        self._intent.pack(fill="x", padx=8, pady=(0, 6))

        self._rows = []
        for _ in range(max_rows):
            lbl = ttk.Label(self, text="", style="Panel.TLabel", font=theme.FONTS["small"],
                             wraplength=ROW_WRAP_PX, justify="left")
            lbl.pack(fill="x", anchor="w", padx=8, pady=1)
            self._rows.append(lbl)

        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=(8, 0))

    def update(self, intent_text: str, conditions: list):
        self._intent.config(text=intent_text)
        total = len(conditions)
        met = sum(1 for c in conditions if c["met"])
        badge_bg = MET_COLOR if total and met == total else UNMET_COLOR
        self._badge.config(text=f"{met}/{total}", bg=badge_bg)
        for i, lbl in enumerate(self._rows):
            if i >= len(conditions):
                lbl.config(text="")
                continue
            c = conditions[i]
            if c["met"]:
                mark, color = "✓", MET_COLOR
            elif c["key"] in BLOCKING_KEYS:
                mark, color = "✗", BLOCK_COLOR
            else:
                mark, color = "✗", UNMET_COLOR
            lbl.config(text=f"{mark} {c['label']} — {c['detail']}", foreground=color)
