"""메인 윈도우 — 탭(Notebook)과 EventBus 폴링 루프를 구성한다.

스레드 규칙: 위젯을 만지는 코드는 전부 메인 스레드에서만 실행된다.
root.after(POLL_MS, self._poll)로 100ms마다 EventBus.dispatch()를 호출해
백그라운드 스레드가 쌓아둔 이벤트를 메인 스레드에서 안전하게 소비한다.
"""

import tkinter as tk
from tkinter import ttk

from gui import theme
from gui.event_bus import EventBus
from gui.controllers.bot_controller import BotController
from gui.views.dashboard_page import DashboardPage
from gui.views.trading_page import TradingPage
from gui.views.log_page import LogPage
from gui.views.settings_page import SettingsPage

POLL_MS = 100


class MainWindow:
    def __init__(self, root, autostart_live=False):
        self.root = root
        self.root.title("청산 흐름 전략 — 실시간 대시보드 (섀도 전용, 실주문 없음)")
        self.root.geometry("1280x800")
        theme.apply_theme(self.root)

        self.bus = EventBus()
        self.controller = BotController(self.bus)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        self.dashboard = DashboardPage(notebook, self.bus, self.controller)
        self.trading = TradingPage(notebook, self.bus, self.controller)
        self.settings = SettingsPage(notebook, self.bus, self.controller)
        self.log = LogPage(notebook, self.bus, self.controller)

        notebook.add(self.dashboard.frame, text="Dashboard")
        notebook.add(self.trading.frame, text="Trading")
        notebook.add(self.settings.frame, text="Settings")
        notebook.add(self.log.frame, text="Log")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

        if autostart_live:
            self.trading.start_live()

    def _poll(self):
        self.bus.dispatch()
        self.root.after(POLL_MS, self._poll)

    def _on_close(self):
        self.controller.stop()
        self.root.destroy()
