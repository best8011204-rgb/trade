"""GUI 진입점.

실행:
    python3 -m gui.app                 # 창이 뜨자마자 Live 모드로 자동 시작
    python3 -m gui.app --synthetic     # 자동 시작 없이 수동으로 모드 선택

Live 모드는 Binance 실시간 웹소켓 섀도 트레이딩(실주문 없음)이다. 인터넷이
열린 환경(로컬 PC/VPS)에서만 실제로 연결된다 — 아웃바운드가 막힌 샌드박스
에서는 연결 시도만 하고 재시도를 반복한다.

Linux에서 tkinter가 없다면: apt-get install python3-tk
"""

import argparse
import tkinter as tk

from gui.main_window import MainWindow


def main():
    ap = argparse.ArgumentParser(description="청산 흐름 전략 GUI")
    ap.add_argument("--synthetic", action="store_true",
                     help="자동 시작을 끄고 합성 데이터 모드로 대기 (수동으로 모드/Start 선택)")
    args = ap.parse_args()

    root = tk.Tk()
    MainWindow(root, autostart_live=not args.synthetic)
    root.mainloop()


if __name__ == "__main__":
    main()
