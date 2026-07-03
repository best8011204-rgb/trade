"""Strategy 페이지 — Setup A/B 파라미터 폼을 dataclass로부터 자동 생성.

liquidation_strategy.setup_a.CascadeAParams / setup_b.CascadeBParams에 필드를
추가/삭제해도 이 페이지 코드는 수정할 필요가 없다 — dataclasses.fields()로
읽어서 그대로 입력폼을 만든다.
"""

import dataclasses
from tkinter import ttk, messagebox

from gui.controllers import strategy_controller as sc


class StrategyPage:
    def __init__(self, parent, bus, controller):
        self.frame = ttk.Frame(parent)
        self.controller = controller
        self.entries = {"A": {}, "B": {}}

        notebook = ttk.Notebook(self.frame)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        for setup, title in (
            ("A", "Setup A (Cascade Exhaustion Long)"),
            ("B", "Setup B (Trapped-Long Flush Short)"),
        ):
            tab = ttk.Frame(notebook)
            notebook.add(tab, text=title)
            self._build_form(tab, setup)

    def _build_form(self, tab, setup):
        defaults = sc.default_instance(setup)
        fields = dataclasses.fields(defaults)
        for i, f in enumerate(fields):
            ttk.Label(tab, text=f.name).grid(row=i, column=0, padx=5, pady=3, sticky="w")
            entry = ttk.Entry(tab, width=20)
            entry.insert(0, str(getattr(defaults, f.name)))
            entry.grid(row=i, column=1, padx=5, pady=3, sticky="w")
            self.entries[setup][f.name] = entry

        save_btn = ttk.Button(tab, text="Save", command=lambda s=setup: self._save(s))
        save_btn.grid(row=len(fields), column=0, columnspan=2, pady=10)

    def _save(self, setup):
        if self.controller.is_running:
            messagebox.showwarning(
                "적용 불가", "봇 실행 중에는 전략 파라미터를 변경할 수 없습니다. 먼저 Stop 하세요.")
            return
        values = {name: entry.get() for name, entry in self.entries[setup].items()}
        try:
            instance = sc.build_instance(setup, values)
        except (ValueError, TypeError) as e:
            messagebox.showerror("입력 오류", f"파라미터 값이 올바르지 않습니다: {e}")
            return
        if setup == "A":
            self.controller.set_strategy_params(a_params=instance)
        else:
            self.controller.set_strategy_params(b_params=instance)
        messagebox.showinfo("저장 완료", f"Setup {setup} 파라미터가 적용되었습니다. (다음 Start부터 반영)")
