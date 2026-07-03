"""GUI 진입점.

실행: python3 -m gui.app
"""

import tkinter as tk

from gui.views.main_window import MainWindow


def main():
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
