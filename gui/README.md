# Trade Engine GUI

`liquidation_strategy` 엔진(Setup A/B, `StrategyEngine`)을 표시/제어하는 tkinter GUI.

## 설계 원칙

- GUI는 `BotController`를 통해서만 엔진을 시작/정지한다 — 엔진을 직접 호출하지 않는다.
- 엔진(백그라운드 스레드)과 GUI(메인 스레드)는 `EventBus` 하나로만 통신한다.
  백그라운드 스레드는 `bus.publish(topic, data)`만 호출하고, 메인 스레드는
  `root.after(100, ...)`로 `bus.dispatch()`를 폴링해 구독자(페이지/위젯)에게 전달한다.
  tkinter 위젯은 메인 스레드에서만 안전하게 조작할 수 있으므로, 이 큐 브리지가
  유일한 스레드 경계 통로다.
- Setup A/B 파라미터(`CascadeAParams`/`CascadeBParams`)는 dataclass 그대로 읽어
  Strategy 페이지 폼을 자동 생성한다. 새 파라미터 필드를 추가해도 GUI 코드 수정이
  필요 없다.

## 실행

```bash
pip install -r requirements.txt   # numpy, pandas, requests, websockets
python3 -m gui.app
```

tkinter는 표준 라이브러리이지만 배포판에 따라 별도 패키지 설치가 필요할 수 있다
(Debian/Ubuntu: `apt install python3-tk`).

## 현재 데이터 소스: 합성 데이터 시뮬레이션

Trading 페이지에서 Start를 누르면 `simulate_data.generate()`로 만든 합성 스트림을
실제 `StrategyEngine`에 흘려 넣는다. 실주문은 없다(API 키도 필요 없다). 이벤트
병합 순서는 `liquidation_strategy/backtest.py`의 검증된 규칙(동일 타임스탬프에서
force_order/cvd를 candle보다 먼저 처리)을 그대로 따른다.

## 실거래소 실시간 연동 (향후)

`liquidation_strategy/live_feed.py`(`LiveShadowRunner`)는 인터넷이 열린 환경에서
Binance 공개 웹소켓에 연결해 동일한 엔진을 구동한다. 이 GUI에 연결하려면
`gui/engine_bridge.py`에 `LiveEngineRunner`를 추가해 `LiveShadowRunner`의 콜백
지점(`on_force_order_msg`, `on_kline_msg`, `_flush_new_logs` 등)에서 동일한
토픽(`status`/`candle`/`position`/`signal`/`trade_closed`/`summary`)으로
`bus.publish()`를 호출하면 된다 — views/widgets 쪽은 수정할 필요가 없다.

## 폴더 구조

```
gui/
├── app.py                      # 진입점 (python3 -m gui.app)
├── event_bus.py                # 스레드 안전 pub/sub 브리지
├── engine_bridge.py             # StrategyEngine을 백그라운드 스레드에서 구동
├── controllers/
│   ├── bot_controller.py        # Start/Stop 생명주기 관리
│   └── strategy_controller.py   # 파라미터 dataclass <-> 폼 값 변환
├── views/
│   ├── main_window.py           # 좌측 메뉴 + 페이지 전환 + dispatch 폴링
│   ├── dashboard_page.py        # 상태/가격/박스/포지션/누적성과 (읽기 전용)
│   ├── trading_page.py          # 심볼/배속/기간 설정 + Start/Stop
│   ├── strategy_page.py         # Setup A/B 파라미터 자동 생성 폼
│   └── log_page.py              # 시그널/체결 로그 + 체결 이력 테이블
└── widgets/
    ├── status_widget.py
    ├── position_widget.py
    ├── pnl_widget.py
    └── trades_table.py
```
