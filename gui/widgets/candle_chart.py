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
BOX_COLOR = "#f0b90b"   # Binance yellow(주황) — Setup B 4h 박스 상/하단
OI_COLOR = "#f0b90b"    # OI 라인 (Binance 지표 골드)
OI_FILL = "#3a3420"     # OI 영역 채움 (골드 저채도)
OI_PANEL_FRAC = 0.24    # 캔버스 높이 중 OI 서브패널 비중
MAX_OI_KEPT = 2000      # 보관할 OI 포인트 수
VOLUME_PANEL_FRAC = 0.16  # 캔버스 높이 중 거래량 서브패널 비중

# 보유 포지션 SL/TP 점선 (setup 무관 공통)
SL_COLOR = "#f6465d"    # 빨강 — 손절가
TP1_COLOR = "#29b6f6"   # 파랑 — 1차 목표가
TP2_COLOR = "#ab47bc"   # 보라 — 2차 목표가
# 전략이 아직 포지션 없이 '주시 중'인 참고가 (설명은 Setup.describe() 참고)
A_WATCH_COLOR = "#26a69a"  # 청록 — Setup A 캐스케이드 저점
B_WATCH_COLOR = "#ec407a"  # 핑크 — Setup B 돌파 고점(sweep high)


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
        self._oi = []                                       # [(ts, oi)] 시간순
        self._legs = []                                      # 보유 포지션 [{sl, tp1, tp2, ...}]
        self._watch = {"a": None, "b": None}                # Setup A/B가 주시 중인 참고가

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

    def set_legs(self, open_legs):
        """보유 중인 포지션(트랜치) 목록. 각 dict에 sl/tp1/tp2 가격 포함."""
        self._legs = list(open_legs)
        self._mark_dirty()

    def set_watch(self, key, price):
        """전략이 포지션 진입 전 주시 중인 참고가. key: 'a' | 'b'."""
        if self._watch.get(key) != price:
            self._watch[key] = price
            self._mark_dirty()

    def add_oi(self, ts, oi):
        """OI 포인트 1건 (라이브 5분 폴링 / 합성 oi_points)."""
        if self._oi and ts <= self._oi[-1][0]:
            if ts == self._oi[-1][0]:
                self._oi[-1] = (ts, oi)   # 같은 시각 재수신 시 교체
                self._mark_dirty()
            return
        self._oi.append((ts, oi))
        del self._oi[:-MAX_OI_KEPT]
        self._mark_dirty()

    def set_oi_history(self, points):
        """REST 백필 결과 [(ts, oi)]로 OI 버퍼를 통째로 초기화."""
        self._oi = sorted(points, key=lambda p: p[0])[-MAX_OI_KEPT:]
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
        # SL/TP/관찰 레벨도 화면 밖으로 벗어나지 않게 범위에 포함
        extra_levels = [v for v in self._watch.values() if v is not None]
        for leg in self._legs:
            extra_levels += [v for v in (leg.get("sl"), leg.get("tp1"), leg.get("tp2")) if v is not None]
        if extra_levels:
            y_min = min(y_min, min(extra_levels))
            y_max = max(y_max, max(extra_levels))
        span = max(y_max - y_min, 1e-9)
        y_min -= span * 0.05
        y_max += span * 0.05
        span = y_max - y_min

        plot_w = W - PAD_L - PAD_R
        avail_h = H - PAD_T - PAD_B
        # 하단에 거래량 서브패널(항상) + OI 서브패널(데이터 있을 때만) 분리
        vol_h = int(avail_h * VOLUME_PANEL_FRAC)
        vol_gap = 8
        oi_h = int(avail_h * OI_PANEL_FRAC) if self._oi else 0
        oi_gap = 8 if oi_h else 0
        plot_h = avail_h - vol_h - vol_gap - oi_h - oi_gap
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

        # 4h 박스 상/하단 (주황 점선, Setup B가 돌파를 감시하는 레인지)
        for p, lbl in ((bh, "박스상단(B)"), (bl, "박스하단(B)")):
            if p is not None and y_min <= p <= y_max:
                cv.create_line(PAD_L, Y(p), W - PAD_R, Y(p), fill=BOX_COLOR, dash=(4, 3))
                cv.create_text(W - PAD_R + 6, Y(p), text=lbl, fill=BOX_COLOR,
                               anchor="w", font=("Arial", 8))

        # Setup A/B가 포지션 진입 전 주시 중인 참고가 (청록/핑크 점선)
        for key, color, lbl in (("a", A_WATCH_COLOR, "A 저점(관찰)"), ("b", B_WATCH_COLOR, "B 돌파고점(관찰)")):
            p = self._watch.get(key)
            if p is not None and y_min <= p <= y_max:
                cv.create_line(PAD_L, Y(p), W - PAD_R, Y(p), fill=color, dash=(5, 3))
                cv.create_text(W - PAD_R + 6, Y(p), text=lbl, fill=color,
                               anchor="w", font=("Arial", 8))

        # 보유 포지션 SL/TP1/TP2 (빨강/파랑/보라 점선)
        drawn = set()
        for leg in self._legs:
            for price, color, lbl in (
                (leg.get("sl"), SL_COLOR, "SL"),
                (leg.get("tp1"), TP1_COLOR, "TP1"),
                (leg.get("tp2"), TP2_COLOR, "TP2"),
            ):
                if price is None:
                    continue
                dedup_key = (round(price, 1), color)
                if dedup_key in drawn or not (y_min <= price <= y_max):
                    continue
                drawn.add(dedup_key)
                cv.create_line(PAD_L, Y(price), W - PAD_R, Y(price), fill=color, dash=(6, 2))
                cv.create_text(W - PAD_R + 6, Y(price), text=lbl, fill=color,
                               anchor="w", font=("Arial", 8, "bold"))

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

        # 현재가 라인 — 전략 레벨이 아니라 마지막 종가 표시선. 마지막 봉이
        # 양봉이면 녹색, 음봉이면 빨강으로 그려질 뿐 별도 의미는 없다.
        last = bars[-1]
        y_last = Y(last["close"])
        last_color = UP_COLOR if last["close"] >= last["open"] else DOWN_COLOR
        cv.create_line(PAD_L, y_last, W - PAD_R, y_last, fill=last_color, dash=(1, 3))
        cv.create_text(W - PAD_R + 6, y_last, text="현재가", fill=last_color,
                       anchor="w", font=("Arial", 8))

        # ---- 거래량 서브패널 (캔들 바로 아래, Binance 스타일) ----
        vol_top = PAD_T + plot_h + vol_gap
        vol_txt = self._draw_volume_panel(cv, bars, slot, PAD_L, PAD_R, W, vol_top, vol_h)

        # ---- OI 서브패널 (Binance 오픈 인터레스트 지표 스타일) ----
        oi_txt = ""
        if oi_h:
            oi_top = vol_top + vol_h + oi_gap
            oi_txt = self._draw_oi_panel(cv, bars, slot, PAD_L, PAD_R, W, oi_top, oi_h)

        src = "실데이터" if self._has_native[iv] else "1m 집계(합성)"
        self._info.config(text=f"{iv} · {n}봉 · 종가 {last['close']:,.1f}{vol_txt}{oi_txt} · {src}")

    def _draw_volume_panel(self, cv, bars, slot, PAD_L, PAD_R, W, top, h):
        """캔들과 동일 X축의 거래량 막대 서브차트. 반환: 인포바용 텍스트."""
        vols = [b.get("volume", 0.0) for b in bars]
        v_max = max(vols) if vols else 0.0
        cv.create_line(PAD_L, top, W - PAD_R, top, fill=GRID_COLOR)
        cv.create_text(PAD_L + 4, top + 4, text="VOL", fill=TEXT_COLOR,
                       anchor="nw", font=("Arial", 8, "bold"))
        if v_max <= 0:
            return ""
        body_w = max(1, min(slot * 0.7, 12))
        base = top + h
        for i, b in enumerate(bars):
            x = PAD_L + slot * i + slot / 2
            vh = (b.get("volume", 0.0) / v_max) * (h - 6)
            color = UP_COLOR if b["close"] >= b["open"] else DOWN_COLOR
            cv.create_rectangle(x - body_w / 2, base - vh, x + body_w / 2, base,
                                fill=color, outline=color)
        cv.create_text(W - PAD_R + 6, top + 4, text=_fmt_oi(v_max), fill=TEXT_COLOR,
                       anchor="nw", font=("Arial", 8))
        last_vol = bars[-1].get("volume", 0.0)
        return f" · 거래량 {_fmt_oi(last_vol)}"

    def _draw_oi_panel(self, cv, bars, slot, PAD_L, PAD_R, W, top, h):
        """캔들 X축과 시간 정렬된 OI 라인+영역 서브차트. 반환: 인포바용 텍스트."""
        t0, t1 = bars[0]["ts"], bars[-1]["ts"]
        # 화면 구간 밖 직전 포인트 1개를 포함해 라인이 왼쪽 끝까지 이어지게 한다
        pts = [p for p in self._oi if p[0] <= t1]
        first_in = next((i for i, p in enumerate(pts) if p[0] >= t0), None)
        if first_in is None:
            pts = pts[-1:]
        elif first_in > 0:
            pts = pts[first_in - 1:]
        if not pts:
            return ""

        vals = [v for _, v in pts]
        v_min, v_max = min(vals), max(vals)
        span = max(v_max - v_min, max(abs(v_max), 1e-9) * 1e-4)
        v_min -= span * 0.08
        v_max += span * 0.08
        span = v_max - v_min

        cv.create_line(PAD_L, top, W - PAD_R, top, fill=GRID_COLOR)

        span_t = max(t1 - t0, 1e-9)
        x_right = PAD_L + slot * (len(bars) - 1) + slot / 2

        def X(ts):
            return min(PAD_L + (ts - t0) / span_t * (x_right - PAD_L), x_right)

        def Y(v):
            return top + (v_max - v) / span * h

        xy = [(max(X(ts), PAD_L), Y(v)) for ts, v in pts]
        if len(xy) >= 2:
            base = top + h
            poly = [(xy[0][0], base)] + xy + [(xy[-1][0], base)]
            cv.create_polygon(*[c for p in poly for c in p], fill=OI_FILL, outline="")
            cv.create_line(*[c for p in xy for c in p], fill=OI_COLOR, width=1)
        last_x, last_y = xy[-1]
        cv.create_oval(last_x - 2, last_y - 2, last_x + 2, last_y + 2,
                       fill=OI_COLOR, outline=OI_COLOR)

        cv.create_text(PAD_L + 4, top + 4, text="OI", fill=OI_COLOR,
                       anchor="nw", font=("Arial", 8, "bold"))
        for v, anchor_y in ((v_max, top), (v_min, top + h)):
            cv.create_text(W - PAD_R + 6, max(min(anchor_y, top + h - 5), top + 5),
                           text=_fmt_oi(v), fill=TEXT_COLOR, anchor="w", font=("Arial", 8))
        last_oi = pts[-1][1]
        cv.create_text(W - PAD_R + 6, last_y, text=_fmt_oi(last_oi),
                       fill=OI_COLOR, anchor="w", font=("Arial", 8, "bold"))
        return f" · OI {_fmt_oi(last_oi)}"


def _fmt_oi(v):
    """Binance식 축약 표기 (86.53K, 1.24M)."""
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:.2f}K"
    return f"{v:,.2f}"
