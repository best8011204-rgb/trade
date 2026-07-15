"""Settings 페이지 — Setup A / Setup B 전략 파라미터를 GUI에서 조정.

CascadeAParams/CascadeBParams(둘 다 dataclass)의 필드를 dataclasses.fields()로
읽어 자동으로 입력폼을 만든다. 새 필드가 추가되면 이 페이지도 자동으로
반영되므로 setup_a.py/setup_b.py를 고칠 때 여기를 따로 손댈 필요가 없다.

"적용"을 누르면 BotController.set_strategy_params()에 새 CascadeAParams/
CascadeBParams 인스턴스를 넘긴다 — 다음 Start부터 적용되며, 이미 실행 중인
러너에는 영향을 주지 않는다(엔진/러너는 시작 시점에 파라미터를 스냅샷으로
받아가는 구조이기 때문).
"""

import dataclasses
import tkinter as tk
from tkinter import ttk, messagebox

from liquidation_strategy.setup_a import CascadeAParams
from liquidation_strategy.setup_b import CascadeBParams


class ParamGroup:
    """dataclass 하나(CascadeAParams 또는 CascadeBParams)에 대한 입력폼."""

    def __init__(self, parent, title, param_cls):
        self.param_cls = param_cls
        self.frame = ttk.LabelFrame(parent, text=title)
        self.entries = {}

        defaults = param_cls()
        for row, f in enumerate(dataclasses.fields(param_cls)):
            ttk.Label(self.frame, text=f.name).grid(
                row=row, column=0, padx=8, pady=2, sticky="w")
            entry = ttk.Entry(self.frame, width=14)
            entry.insert(0, str(getattr(defaults, f.name)))
            entry.grid(row=row, column=1, padx=8, pady=2, sticky="w")
            self.entries[f.name] = (entry, f.type)

    def read(self):
        """입력값을 파싱해 새 dataclass 인스턴스를 만든다. 실패 시 ValueError."""
        kwargs = {}
        for name, (entry, ftype) in self.entries.items():
            raw = entry.get().strip()
            try:
                kwargs[name] = ftype(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{name}: '{raw}' 은(는) {ftype.__name__} 형식이 아닙니다.")
        return self.param_cls(**kwargs)

    def reset(self):
        defaults = self.param_cls()
        for name, (entry, _) in self.entries.items():
            entry.delete(0, tk.END)
            entry.insert(0, str(getattr(defaults, name)))


class SettingsPage:
    def __init__(self, parent, bus, controller):
        self.controller = controller
        self.frame = ttk.Frame(parent)

        note = ttk.Label(
            self.frame,
            text="Setup A/B 전략 파라미터. \"적용\"은 다음 Start부터 반영되며, "
                 "이미 실행 중인 러너에는 영향을 주지 않습니다.",
            foreground="gray", wraplength=900, justify="left")
        note.pack(anchor="w", padx=10, pady=(10, 5))

        groups = ttk.Frame(self.frame)
        groups.pack(fill="both", expand=True, padx=10)
        groups.columnconfigure(0, weight=1)
        groups.columnconfigure(1, weight=1)

        self.group_a = ParamGroup(groups, "Setup A — 캐스케이드 소진 롱", CascadeAParams)
        self.group_a.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.group_b = ParamGroup(groups, "Setup B — 트랩드롱 플러시 숏", CascadeBParams)
        self.group_b.frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        control = ttk.Frame(self.frame)
        control.pack(fill="x", padx=10, pady=10)
        ttk.Button(control, text="적용", command=self._apply).pack(side="left", padx=5)
        ttk.Button(control, text="기본값 복원", command=self._reset).pack(side="left", padx=5)

        self.status = ttk.Label(self.frame, text="", foreground="gray")
        self.status.pack(anchor="w", padx=10)

    def _apply(self):
        try:
            a_params = self.group_a.read()
            b_params = self.group_b.read()
        except ValueError as e:
            messagebox.showwarning("입력 오류", str(e))
            return
        self.controller.set_strategy_params(a_params=a_params, b_params=b_params)
        running_note = " (실행 중인 러너에는 다음 Start부터 적용됩니다)" if self.controller.is_running else ""
        self.status.config(text=f"적용됨.{running_note}")

    def _reset(self):
        self.group_a.reset()
        self.group_b.reset()
        self.status.config(text="기본값으로 되돌렸습니다 (아직 적용 전 — \"적용\"을 눌러야 반영됩니다).")
