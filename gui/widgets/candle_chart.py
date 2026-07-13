"""tk.Canvas 기반 실시간 캔들차트 위젯 (외부 의존성 없음).

- 타임프레임(1m/5m/1h/1d)별 확정봉 버퍼를 따로 유지하고, 선택된 타임프레임만
  그린다.
- 데이터 유입 경로 두 가지:
    * add_native(interval, candle): Binance가 준 해당 타임프레임의 '진짜'
      확정봉 (라이브 모드의 candle_tf / candle_history 이벤트).
    * add_1m(candle): 1m 봉 하나. 네이티브 봉이 한 번도 안 들어온
      타임프레임에 한해 1m -> 5m/1h/1d 집계봉을 만든다 (합성 데이터 모드용).
      네이티브 봉이 들어오기 시작한 타임프레임은 집계를 중단한다 —
      두 소스가 섞여 봉이 중복 생성되는 것을 막기 위함이다.
- 그리기 부하 제어: 데이터가 들어올 때마다 즉시 그리지 않고 dirty 플래그만
  세우고, after(REDRAW_MS) 타이머가 일괄 리드로우한다.

주의: 집계봉(합성 모드)의 5m/1h/1d 버킷 경계는 UTC 타임스탬프 기준
정수 나눗셈으로 정한다. Binance kline 경계와 동일한 규칙이다.
"""

import tkinter as tk
from tkinter import ttk

INTERVALS = ("1m", "5m", "1h", "1d")
INTERVAL_S = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}
MAX_BARS_KEPT = 500     # 타임프레임별 보관 봉 수
VISIBLE_BARS = 120      # 화면에 그릴 최근 봉 수
REDRAW_MS = 250         # 리드로우 최소 간격

UP_COLOR = "#0ecb81"    # Binance green
DOWN_COLOR = "#f6465d"  # Binance red
BG_COLOR = "#161a1e"
GRID_COLOR = "#2b3139"
TEXT_COLOR = "#848e9c"
BOX_COLOR = "#f0b90b"   # Binance yellow — 4h 박스 상/하단


class CandleChart(ttk.Frame):
    def __init__(self, parent, height=320):
        super().__init__(parent)

        # ---- 타임프레임 선택 바 ----
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self._tf_var = tk.StringVar(value="1m")
        for iv in INTERVALS:
            ttk.Radiobutton(bar, text=iv, value=iv, variable=self._tf_var,
                            command=self._mark_dirty).pack(side="left", padx=2)
        self._info = ttk.Label(bar, text="", foreground="gray")
        self._info.pack(side="right", padx=6)

        # ---- 캔버스 ----
        self.canvas = tk.Canvas(self, height=height, bg=BG_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._mark_dirty())

        # ---- 데이터 버퍼 ----
        self._bars = {iv: [] for iv in INTERVALS}          # 확정봉 목록
        self._forming = {iv: None for iv in INTERVALS}     # 집계 중 미확정 봉 (합성 모드)
        self._has_native = {iv: False for iv in INTERVALS} # 네이티브 봉 수신 여부
        self._box = (None, None)                            # (box_low, box_high)

        self._dirty = False
        self._after_id = self.after(REDRAW_MS, self._redraw_loop)
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, _event=None):
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    # ------------------------------------------------------------------
    # 데이터 유입
    # ------------------------------------------------------------------
    def add_native(self, interval, candle):
        """해당 타임프레임의 진짜 확정봉 (라이브 웹소켓 / REST 백필)."""
        if interval not in self._bars:
            return
        self._has_native[interval] = True
        self._forming[interval] = None  # 네이티브 우선 — 집계봉 폐기
        bars = self._bars[interval]
        if bars and bars[-1]["ts"] == candle["ts"]:
            bars[-1] = candle          # 같은 봉 재수신 시 교체
        elif bars and candle["ts"] < bars[-1]["ts"]:
            return                     # 과거 봉 뒤늦은 도착은 무시
        else:
            bars.append(candle)
        del bars[:-MAX_BARS_KEPT]
        self._mark_dirty()

    def set_history(self, interval, candles):
        """REST 백필 결과로 버퍼를 통째로 초기화."""
        if interval not in self._bars:
            return
        self._has_native[interval] = True
        self._bars[interval] = list(candles)[-MAX_BARS_KEPT:]
        self._forming[interval] = None
        self._mark_dirty()

    def add_1m(self, candle):
        """1m 확정봉 하나. 네이티브가 없는 타임프레임만 집계 생성 (합성 모드)."""
        if not self._has_native["1m"]:
            self._append_confirmed("1m", candle)
        for iv in ("5m", "1h", "1d"):
            if self._has_native[iv]:
                continue
            self._aggregate(iv, candle)
        self._mark_dirty()

    def set_box(self, box_low, box_high):
        self._box = (box_low, box_high)
        self._mark_dirty()

    # ------------------------------------------------------------------
    def _append_confirmed(self, interval, candle):
        bars = self._bars[interval]
        if bars and candle["ts"] <= bars[-1]["ts"]:
            return
        bars.append(dict(candle))
        del bars[:-MAX_BARS_KEPT]

    def _aggregate(self, interval, m1):
        """1m 봉을 interval 버킷에 누적. 버킷이 넘어가면 이전 버킷을 확정."""
        sec = INTERVAL_S[interval]
        bucket_ts = (int(m1["ts"]) // sec) * sec
        f = self._forming[interval]
        if f is None or f["ts"] != bucket_ts:
            if f is not None:
                self._append_confirmed(interval, f)  # 이전 버킷 확정
            self._forming[interval] = {
                "ts": bucket_ts, "open": m1["open"], "high": m1["high"],
                "low": m1["low"], "close": m1["close"], "volume": m1["volume"],
                "closed": False,
            }
        else:
            f["high"] = max(f["high"], m1["high"])
            f["low"] = min(f["low"], m1["low"])
            f["close"] = m1["close"]
            f["volume"] += m1["volume"]

    # ------------------------------------------------------------------
    # 렌더링
    # ------------------------------------------------------------------
    def _mark_dirty(self):
        self._dirty = True

    def _redraw_loop(self):
        if self._dirty:
            self._dirty = False
            try:
                self._draw()
            except tk.TclError:
                return  # 위젯 파괴 후 타이머 잔여 호출
        self._after_id = self.after(REDRAW_MS, self._redraw_loop)

    def _visible_bars(self):
        iv = self._tf_var.get()
        bars = list(self._bars[iv])
        f = self._forming[iv]
        if f is not None:
            bars = bars + [f]
        return iv, bars[-VISIBLE_BARS:]

    def _draw(self):
        cv = self.canvas
        cv.delete("all")
        W = max(cv.winfo_width(), 50)
        H = max(cv.winfo_height(), 50)
        PAD_L, PAD_R, PAD_T, PAD_B = 8, 64, 10, 18

        iv, bars = self._visible_bars()
        if not bars:
            cv.create_text(W / 2, H / 2, text=f"{iv} 데이터 대기 중...", fill=TEXT_COLOR)
            self._info.config(text="")
            return

        lows = [b["low"] for b in bars]
        highs = [b["high"] for b in bars]
        y_min, y_max = min(lows), max(highs)
        bl, bh = self._box
        if bl is not None:
            y_min = min(y_min, bl)
        if bh is not None:
            y_max = max(y_max, bh)
        span = max(y_max - y_min, 1e-9)
        y_min -= span * 0.05
        y_max += span * 0.05
        span = y_max - y_min

        plot_w = W - PAD_L - PAD_R
        plot_h = H - PAD_T - PAD_B
        n = len(bars)
        slot = plot_w / n
        body_w = max(1, min(slot * 0.7, 12))

        def Y(p):
            return PAD_T + (y_max - p) / span * plot_h

        # 가격 그리드 (4분할) + 우측 축 라벨
        for i in range(5):
            p = y_max - span * i / 4
            y = Y(p)
            cv.create_line(PAD_L, y, W - PAD_R, y, fill=GRID_COLOR)
            cv.create_text(W - PAD_R + 6, y, text=f"{p:,.1f}", fill=TEXT_COLOR,
                           anchor="w", font=("Arial", 8))

        # 4h 박스 상/하단
        for p in (bl, bh):
            if p is not None and y_min <= p <= y_max:
                cv.create_line(PAD_L, Y(p), W - PAD_R, Y(p), fill=BOX_COLOR, dash=(4, 3))

        # 캔들
        for i, b in enumerate(bars):
            x = PAD_L + slot * i + slot / 2
            color = UP_COLOR if b["close"] >= b["open"] else DOWN_COLOR
            cv.create_line(x, Y(b["high"]), x, Y(b["low"]), fill=color)
            y0, y1 = Y(b["open"]), Y(b["close"])
            if abs(y1 - y0) < 1:
                y1 = y0 + 1
            cv.create_rectangle(x - body_w / 2, min(y0, y1), x + body_w / 2, max(y0, y1),
                                fill=color, outline=color)
            if not b.get("closed", True):  # 집계 중 미확정 봉 표시
                cv.create_rectangle(x - body_w / 2, min(y0, y1), x + body_w / 2, max(y0, y1),
                                    outline=TEXT_COLOR, dash=(2, 2))

        last = bars[-1]
        y_last = Y(last["close"])
        cv.create_line(PAD_L, y_last, W - PAD_R, y_last,
                       fill=UP_COLOR if last["close"] >= last["open"] else DOWN_COLOR,
                       dash=(1, 3))

        src = "실데이터" if self._has_native[iv] else "1m 집계(합성)"
        self._info.config(text=f"{iv} · {n}봉 · 종가 {last['close']:,.1f} · {src}")
