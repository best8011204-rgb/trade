"""바이낸스 스타일 다크 터미널 팔레트 + ttk 전역 스타일.

`apply_theme(root)`를 Tk 루트 생성 직후, 다른 위젯을 만들기 전에 한 번
호출하면 이 모듈의 색/폰트 상수와 ttk.Style이 앱 전역(모든 탭)에 적용된다.

순수 렌더링 계층이다 — StrategyEngine/LiveEngineRunner/EventBus/live_feed
등 전략·데이터 로직과는 전혀 무관하다.
"""

import tkinter.font as tkfont
from tkinter import ttk

# ---- 팔레트 (바이낸스 다크) --------------------------------------------
PAGE_BG = "#0B0E11"
PANEL_BG = "#181A20"
ROW_HOVER = "#1E2329"
ROW_SELECT = "#2B3139"
BORDER = "#2B3139"

TEXT_PRIMARY = "#EAECEF"
TEXT_SECONDARY = "#B7BDC6"
TEXT_MUTED = "#848E9C"

ACCENT_YELLOW = "#FCD535"
LONG_GREEN = "#0ECB81"
SHORT_RED = "#F6465A"

# ---- 폰트 (설치 여부를 실제로 확인한 뒤 폴백) ---------------------------
FONTS = {}


def _pick_family(available, candidates):
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]  # 폴백도 없으면 마지막 후보 이름 그대로 (Tk가 알아서 대체)


def init_fonts(root):
    available = set(tkfont.families(root))
    body = _pick_family(available, ["IBM Plex Sans", "Malgun Gothic"])
    mono = _pick_family(available, ["IBM Plex Mono", "Consolas"])
    FONTS.clear()
    FONTS.update({
        "body": (body, 10),
        "body_bold": (body, 10, "bold"),
        "small": (body, 9),
        "small_bold": (body, 9, "bold"),
        "heading": (body, 11, "bold"),
        "mono": (mono, 10),
        "mono_bold": (mono, 10, "bold"),
        "mono_small": (mono, 9),
    })
    return FONTS


def apply_theme(root):
    """폰트 해석 + ttk.Style 전역 설정. MainWindow가 다른 위젯보다 먼저 호출한다."""
    init_fonts(root)
    root.configure(bg=PAGE_BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")  # clam이 아니면 일부 플랫폼에서 Treeview 배경색이 안 먹는다
    except Exception:
        pass

    f_body = FONTS["body"]
    f_small = FONTS["small"]
    f_mono = FONTS["mono"]

    style.configure(".", background=PAGE_BG, foreground=TEXT_PRIMARY, font=f_body)

    style.configure("TFrame", background=PAGE_BG)
    style.configure("Panel.TFrame", background=PANEL_BG)

    style.configure("TLabel", background=PAGE_BG, foreground=TEXT_PRIMARY, font=f_body)
    style.configure("Panel.TLabel", background=PANEL_BG, foreground=TEXT_PRIMARY, font=f_body)
    style.configure("Muted.TLabel", background=PANEL_BG, foreground=TEXT_MUTED, font=f_small)
    style.configure("MutedPage.TLabel", background=PAGE_BG, foreground=TEXT_MUTED, font=f_small)
    style.configure("Heading.TLabel", background=PANEL_BG, foreground=TEXT_PRIMARY, font=FONTS["heading"])
    style.configure("Value.TLabel", background=PANEL_BG, foreground=TEXT_PRIMARY,
                    font=(f_mono[0], 13, "bold"))

    style.configure("TButton", background=PANEL_BG, foreground=TEXT_PRIMARY,
                    bordercolor=BORDER, focuscolor=BORDER, font=f_body, padding=6)
    style.map("TButton", background=[("active", ROW_HOVER)])

    style.configure("TRadiobutton", background=PAGE_BG, foreground=TEXT_SECONDARY, font=f_body)
    style.map("TRadiobutton", background=[("active", PAGE_BG)])

    # 캔들차트 타임프레임 탭(1m/5m/1h/1d)을 라디오 대신 버튼처럼 보이게
    style.configure("ChartTab.TRadiobutton", indicatoron=0, padding=(12, 5),
                    background=PANEL_BG, foreground=TEXT_MUTED, borderwidth=0, font=f_small)
    style.map("ChartTab.TRadiobutton",
              background=[("selected", PANEL_BG), ("active", PANEL_BG)],
              foreground=[("selected", ACCENT_YELLOW), ("active", TEXT_SECONDARY)])

    style.configure("TCheckbutton", background=PAGE_BG, foreground=TEXT_SECONDARY, font=f_body)

    style.configure("TEntry", fieldbackground=PANEL_BG, foreground=TEXT_PRIMARY,
                    background=PANEL_BG, bordercolor=BORDER, insertcolor=TEXT_PRIMARY)
    style.configure("TCombobox", fieldbackground=PANEL_BG, foreground=TEXT_PRIMARY,
                    background=PANEL_BG, arrowcolor=TEXT_MUTED, bordercolor=BORDER)
    style.map("TCombobox",
              fieldbackground=[("readonly", PANEL_BG)],
              foreground=[("readonly", TEXT_PRIMARY)])

    style.configure("TLabelframe", background=PAGE_BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=PAGE_BG, foreground=TEXT_SECONDARY, font=f_body)

    style.configure("TScrollbar", background=PANEL_BG, troughcolor=PAGE_BG,
                    bordercolor=BORDER, arrowcolor=TEXT_MUTED, relief="flat")
    style.map("TScrollbar", background=[("active", ROW_HOVER)])
    style.configure("Vertical.TScrollbar", background=PANEL_BG)
    style.configure("Horizontal.TScrollbar", background=PANEL_BG)

    style.configure("TPanedwindow", background=PAGE_BG)

    style.configure("TNotebook", background=PAGE_BG, borderwidth=0, tabmargins=(0, 4, 2, 0))
    style.configure("TNotebook.Tab", background=PAGE_BG, foreground=TEXT_SECONDARY,
                    padding=(16, 8), font=f_body, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", PANEL_BG)],
              foreground=[("selected", ACCENT_YELLOW)])

    style.configure("Treeview", background=PANEL_BG, fieldbackground=PANEL_BG,
                    foreground=TEXT_PRIMARY, bordercolor=BORDER, borderwidth=0,
                    rowheight=22, font=f_mono)
    style.configure("Treeview.Heading", background=PAGE_BG, foreground=TEXT_MUTED,
                    font=(f_small[0], 9, "bold"), relief="flat", borderwidth=0)
    style.map("Treeview.Heading", background=[("active", PAGE_BG)])
    style.map("Treeview",
              background=[("selected", ROW_SELECT)],
              foreground=[("selected", TEXT_PRIMARY)])

    return style
