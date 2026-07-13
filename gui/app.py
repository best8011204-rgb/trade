"""GUI 진입점.

실행:
    python3 -m gui.app

Linux에서 tkinter가 없다면: apt-get install python3-tk
"""

import tkinter as tk

from gui.main_window import MainWindow


def main():
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
