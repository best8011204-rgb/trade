"""단일 진입점: GUI + 실시간 엔진 + 텔레그램 봇을 한 번에 실행한다.

실행:
    python3 run_all.py                 # GUI를 띄우고 Live 모드로 자동 연결 시작
    python3 run_all.py --synthetic     # 자동 연결 없이 합성 데이터 모드로 대기

GUI 창이 뜨는 즉시 BotController가 Live 모드(Binance 실시간 웹소켓,
페이퍼 — 실주문 없음)로 연결을 시작한다. config.json 에
telegram_bot_token 이 있으면 같은 엔진을 공유하는 텔레그램 봇도 함께 떠서
/status /pnl /set /pause 등으로 현황 확인·전략 수정이 가능하다.
상태는 Dashboard 탭 실시간 캔들차트/포지션/PnL, Log 탭 시그널 스트림에서
바로 확인할 수 있다.

주의: run_all 과 run_bot(헤드리스)을 동시에 실행하지 말 것 — 같은 텔레그램
토큰을 두 프로세스가 폴링하면 충돌한다.

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
