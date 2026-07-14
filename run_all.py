"""단일 진입점: GUI + live_feed(실시간 Binance 섀도)를 한 번에 실행한다.

실행:
    python3 run_all.py                 # GUI를 띄우고 Live 모드로 자동 연결 시작
    python3 run_all.py --synthetic     # 자동 연결 없이 합성 데이터 모드로 대기

GUI 창이 뜨는 즉시 BotController가 Live 모드(Binance 실시간 웹소켓 섀도,
실주문 없음)로 live_feed 연결을 시작한다 — Trading 탭에서 따로 Start를
누를 필요가 없다. 이후 상태는 Dashboard 탭 실시간 캔들차트/포지션/PnL,
Log 탭 시그널 스트림에서 바로 확인할 수 있다.

인터넷이 열린 로컬 PC/VPS에서 실행해야 실제로 데이터가 들어온다 — 이
저장소를 만든 샌드박스는 바이낸스 아웃바운드가 막혀 있어 연결되지 않는다
(README 참고).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui.app import main

if __name__ == "__main__":
    main()
