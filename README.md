# BTC 청산 흐름 전략 v1.0 — 알고리즘 구현 + 검증 대시보드

`liquidationstrategyv1preregistration.md` 사전등록 명세를 코드로 옮긴 구현체.

- **Setup A** — 청산 캐스케이드 소진 롱 (`liquidation_strategy/setup_a.py`)
- **Setup B** — 트랩드롱 플러시 숏 (`liquidation_strategy/setup_b.py`)
- **엔진** — 두 셋업의 상호배제/TP2 동적전환 규칙 (`liquidation_strategy/engine.py`)
- **임계값 캘리브레이션** — 그리드서치로 트리거 감지 임계값만 탐색 (진입/청산 구조는 고정) (`liquidation_strategy/optimize.py`)
- **합성 데이터** — 실 스트림 연결 전 파이프라인 검증용 (`liquidation_strategy/simulate_data.py`)
- **실데이터 REST 클라이언트** — Binance 공개 REST 래퍼 (`liquidation_strategy/binance_client.py`)
- **실시간 섀도 러너** — forceOrder/aggTrade/kline 웹소켓 + OI 폴링 → 엔진 → JSONL 로그 (`liquidation_strategy/live_feed.py`)
- **Setup B 실데이터 백필** — 과거 klines+OI로 Setup B만 실데이터 백테스트 (`liquidation_strategy/backfill.py`)
- **결과 → 대시보드 JSON 스키마 공용 변환** (`liquidation_strategy/report.py`)

## 실행 (합성 데이터, 어디서나 동작)

```bash
pip install -r requirements.txt
python3 -m liquidation_strategy.run                # liquidation_strategy_output.json 생성
python3 -m liquidation_strategy.gui --open          # GUI 서버 (http://127.0.0.1:8760)
```

`gui.py`는 표준 라이브러리만 쓰는 프론트엔드 서버로, 매 요청마다 산출물
JSON을 다시 읽어 터미널형 대시보드(좌: 파라미터/그리드서치, 중앙: 차트,
우상: 전략 로직, 우하: 결과/기각판정)에 주입한다. `run.py`·`live_feed.py`·
`backfill.py`가 JSON을 갱신하면 **열려 있는 브라우저가 자동 리로드**된다.

```bash
python3 -m liquidation_strategy.gui --data liquidation_strategy_output_live.json  # 라이브 스냅샷 바인딩
python3 -m liquidation_strategy.build_dashboard     # (선택) 정적 단일 HTML 내보내기
```

## Tkinter GUI (한 번에 실행: GUI + 실시간 live_feed)

```bash
pip install -r requirements.txt
python3 run_all.py                 # GUI를 띄우고 Live 모드로 자동 연결 시작
python3 run_all.py --synthetic     # 자동 연결 없이 합성 데이터 모드로 대기 (수동 Start)
```

`run_all.py`(= `python3 -m gui.app`과 동일)는 창이 뜨는 즉시 `gui/` 패키지의
BotController가 Live 모드(Binance 실시간 웹소켓 섀도, 실주문 없음)로
`live_feed`를 자동으로 시작한다 — Trading 탭에서 따로 Start를 누를 필요가
없다. Dashboard 탭에서 실시간 캔들차트/포지션/누적 PnL을, Log 탭에서 시그널
스트림을 바로 확인할 수 있다. 인터넷이 열린 로컬 PC/VPS에서 실행해야 실제로
데이터가 들어온다.

## 실데이터 연동

`binance_client.py`(REST) / `live_feed.py`(실시간 웹소켓)가 실제 Binance
USDT-M 선물 공개 데이터에 연결한다. **API 키는 필요 없다** (공개 마켓 데이터만
사용). 단, 이 코드는 **인터넷이 열려 있는 환경**(로컬 머신, VPS 등)에서
실행해야 한다 — 이 저장소를 만든 샌드박스 세션은 조직 아웃바운드 정책상
`fapi.binance.com` REST가 차단되어 있고, 프록시가 WebSocket 업그레이드
자체를 지원하지 않아 라이브 스트림은 원천적으로 열 수 없다.

### 1) 실시간 섀도 검증 (Setup A + B, 명세서 5장 1단계)

```bash
python3 -m liquidation_strategy.live_feed
# Ctrl-C로 종료. 주기적으로 아래를 갱신한다:
#   liquidation_strategy_output_live.json  (대시보드용 스냅샷)
#   logs/raw_events.jsonl                  (원시 forceOrder 이벤트)
#   logs/signals.jsonl                     (T1~T4/T1~T3 트리거 로그)
#   logs/shadow_trades.jsonl               (가상 체결 트레이드)
```

실주문은 전혀 내지 않는다 — 트리거·체결을 계산만 하고 기록한다(섀도 단계).
`vol_multiplier` 등 T1 임계값 비교에 쓰는 "최근 24h 시간당 평균 청산 금액"은
프로세스 시작 시점부터 라이브로 누적되므로, 최초 24시간은 표본이 얕아
과탐지/과소탐지가 있을 수 있다 — 명세서 4장의 "캘리브레이션 필요" 주의사항과
같은 맥락이다.

대시보드에 실시간 결과를 반영하려면 GUI 서버를 라이브 스냅샷에 바인딩하면 된다
(스냅샷이 갱신될 때마다 브라우저 자동 리로드):

```bash
python3 -m liquidation_strategy.gui --data liquidation_strategy_output_live.json --open
```

### 2) Setup B 과거 데이터 백테스트 (실데이터)

```bash
python3 -m liquidation_strategy.backfill --days 29   # openInterestHist 30일 한도
python3 -m liquidation_strategy.build_dashboard       # DATA_PATH를 backfill 출력으로 바꿔 실행하거나 위와 동일한 방식으로 템플릿에 주입
```

**Setup A는 여기 포함되지 않는다.** Binance는 시장 전체 강제청산 이력에 대한
공개 REST 엔드포인트를 제공하지 않는다(`forceOrder`는 라이브 웹소켓 전용).
Setup A를 실데이터로 검증하려면 `live_feed.py`로 스트림을 계속 녹화해
표본을 축적하거나(권장, 명세서 5장 절차와 일치), Coinglass 등 유료 청산
히트맵 데이터를 붙여야 한다(명세서 4장에서도 청산 히트맵은 "선택" 항목으로
명시).

## 주의

`liquidation_strategy/run.py`(합성 데이터) 결과는 실거래소 접속 없이 명세서의
트리거 정의를 모사한 스트레스 시나리오이며 전략의 실제 수익성을 보장하지
않는다. 실데이터 경로(`live_feed.py`, `backfill.py`)로 얻은 결과라도 명세서
5장의 최소 표본(Setup A 30건 / Setup B 20건)에 도달하기 전까지는 기각조건
판정이 통계적으로 유의미하지 않다 — 대시보드는 표본이 최소치 미만이면
해당 규칙을 "표본 미도달"로 표시해 이를 드러낸다.
