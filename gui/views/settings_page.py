"""Settings 페이지 — Setup A / Setup B 전략 파라미터를 GUI에서 조정.

CascadeAParams/CascadeBParams(둘 다 dataclass)의 필드를 dataclasses.fields()로
읽어 자동으로 입력폼을 만든다. 새 필드가 추가되면 이 페이지도 자동으로
반영되므로 setup_a.py/setup_b.py를 고칠 때 여기를 따로 손댈 필요가 없다.

- "중지" (셋업별 버튼): 실행 중인 러너의 해당 셋업(A 또는 B) 트리거 감지를
  즉시 멈춘다. 그 시점에 진행 중이던 포지션이 있으면 승패/bps 결과를
  기록하지 않고 그냥 버린다 — "적용"을 누르기 전까지는 새 신호를 전혀
  만들지 않는다.
- "적용": 러너가 실행 중이면 BotController.restart_setup()으로 즉시
  반영한다 — 진행 중이던 포지션은 "중지"와 동일하게 기록 없이 버려지고,
  해당 셋업이 초기 상태부터 새 파라미터로 바로 재시작된다. 러너가 실행
  중이 아니면 기존과 동일하게 다음 Start부터 쓰일 파라미터로만 저장된다.
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
            text="Setup A/B 전략 파라미터. 실행 중일 때 \"적용\"을 누르면 즉시 "
                 "새 파라미터로 재시작되고(진행 중 포지션은 기록되지 않고 버려짐), "
                 "실행 중이 아니면 다음 Start부터 반영됩니다. \"중지\"는 해당 셋업만 "
                 "즉시 멈춥니다.",
            foreground="gray", wraplength=900, justify="left")
        note.pack(anchor="w", padx=10, pady=(10, 5))

        groups = ttk.Frame(self.frame)
        groups.pack(fill="both", expand=True, padx=10)
        groups.columnconfigure(0, weight=1)
        groups.columnconfigure(1, weight=1)

        self.group_a = ParamGroup(groups, "Setup A — 캐스케이드 소진 롱", CascadeAParams)
        self.group_a.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        ttk.Button(groups, text="Setup A 중지", command=self._stop_a).grid(
            row=1, column=0, sticky="w", padx=(0, 5), pady=(4, 0))

        self.group_b = ParamGroup(groups, "Setup B — 트랩드롱 플러시 숏", CascadeBParams)
        self.group_b.frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        ttk.Button(groups, text="Setup B 중지", command=self._stop_b).grid(
            row=1, column=1, sticky="w", padx=(5, 0), pady=(4, 0))

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
        if self.controller.is_running:
            self.controller.restart_setup("A", a_params)
            self.controller.restart_setup("B", b_params)
            self.status.config(text="적용됨 — 실행 중인 Setup A/B를 새 파라미터로 즉시 "
                                     "재시작했습니다 (진행 중이던 포지션은 기록되지 않았습니다).")
        else:
            self.controller.set_strategy_params(a_params=a_params, b_params=b_params)
            self.status.config(text="적용됨. (다음 Start부터 반영됩니다)")

    def _reset(self):
        self.group_a.reset()
        self.group_b.reset()
        self.status.config(text="기본값으로 되돌렸습니다 (아직 적용 전 — \"적용\"을 눌러야 반영됩니다).")

    def _stop_a(self):
        self._stop("A")

    def _stop_b(self):
        self._stop("B")

    def _stop(self, setup):
        if not self.controller.is_running:
            messagebox.showinfo("정보", "실행 중인 러너가 없습니다.")
            return
        self.controller.stop_setup(setup)
        self.status.config(text=f"Setup {setup}를 중지했습니다 (진행 중이던 포지션은 기록되지 "
                                 f"않고 버려집니다). 다시 시작하려면 파라미터를 확인하고 "
                                 f"\"적용\"을 누르세요.")
