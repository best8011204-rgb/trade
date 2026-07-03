"""메인 윈도우 — 좌측 메뉴 + 페이지 전환.

GUI는 BotController를 통해서만 엔진에 접근한다. EventBus.dispatch()를
100ms마다 폴링하여, 백그라운드 스레드가 발행한 이벤트를 각 페이지/위젯
구독자에게 메인 스레드에서 안전하게 전달한다.
"""

from tkinter import ttk

from gui.event_bus import EventBus
from gui.controllers.bot_controller import BotController
from gui.views.dashboard_page import DashboardPage
from gui.views.trading_page import TradingPage
from gui.views.strategy_page import StrategyPage
from gui.views.log_page import LogPage

DISPATCH_INTERVAL_MS = 100


class MainWindow:
    PAGES = ("Dashboard", "Trading", "Strategy", "Logs")

    def __init__(self, root):
        self.root = root
        self.root.title("Liquidation Strategy - Trade Engine GUI")
        self.root.geometry("900x680")

        self.bus = EventBus()
        self.controller = BotController(self.bus)

        self._build_layout()
        self._poll()

    def _build_layout(self):
        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)

        menu = ttk.Frame(container, width=150)
        menu.pack(side="left", fill="y")
        menu.pack_propagate(False)

        self.content = ttk.Frame(container)
        self.content.pack(side="left", fill="both", expand=True)

        self.pages = {
            "Dashboard": DashboardPage(self.content, self.bus, self.controller),
            "Trading": TradingPage(self.content, self.bus, self.controller),
            "Strategy": StrategyPage(self.content, self.bus, self.controller),
            "Logs": LogPage(self.content, self.bus, self.controller),
        }

        for name in self.PAGES:
            btn = ttk.Button(menu, text=name, command=lambda n=name: self._show(n))
            btn.pack(fill="x", padx=8, pady=4)

        self._show("Dashboard")

    def _show(self, name):
        for page_name, page in self.pages.items():
            if page_name == name:
                page.frame.pack(fill="both", expand=True)
            else:
                page.frame.pack_forget()

    def _poll(self):
        self.bus.dispatch()
        self.root.after(DISPATCH_INTERVAL_MS, self._poll)
