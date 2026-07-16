# BTC 청산 흐름 전략 v1.0 — 알고리즘 구현 + 검증 대시보드

`liquidationstrategyv1preregistration.md` 사전등록 명세를 코드로 옮긴 구현체.

## 🤖 24시간 봇 + GUI + 텔레그램 — 로컬 실행

노트북/PC에서 24시간 돌리는 트레이딩 봇. 전략 엔진(Setup A/B)이 실시간
바이낸스 스트림에서 진입/청산을 **가상 체결(페이퍼)** 하고, 모든
진입/청산/오류를 텔레그램으로 알린다. 텔레그램에서 현황 조회와
**전략 파라미터 실시간 수정**도 가능하다.

> ⚠️ **실주문 경로는 현재 비활성화되어 있다.** `config.json` 의
> `live_trade` 값과 무관하게 실제 주문은 나가지 않는다 (주문 실행 코드는
> `liquidation_strategy/binance_trader.py` 에 남아 있으며 추후 재활성화
> 가능).

### 준비

**텔레그램 봇 생성** — 텔레그램에서 `@BotFather`에게 `/newbot` →
받은 토큰을 config에 넣는다.

### 실행 — `run_all.py` 하나로 전부

```bash
pip install -r requirements.txt
cp config.example.json config.json    # telegram_bot_token 채우기
python3 run_all.py                    # GUI + 실시간 엔진 + 텔레그램 봇
```

`run_all.py` 가 GUI 창(실시간 차트/포지션/PnL/로그)을 띄우면서, 같은 전략
엔진을 공유하는 텔레그램 봇도 함께 시작한다. GUI 없이 서버에서 돌리려면
`python3 run_bot.py`(헤드리스, 기능 동일)를 대신 쓰면 된다 — **둘을 동시에
실행하지는 말 것** (같은 텔레그램 토큰을 두 프로세스가 폴링하면 충돌).

`config.json` 주요 항목:

| 키 | 설명 |
|---|---|
| `telegram_bot_token` | BotFather가 준 토큰 (비우면 텔레그램 비활성) |
| `telegram_chat_id` | 비우면 최초 `/start` 보낸 사용자를 자동 바인딩 |
| `trade_usdt` | 포지션 1개당 명목가(USDT) — 페이퍼 표기용 |
| `live_trade` | **현재 무시됨** (실주문 비활성) |

봇을 시작한 뒤 텔레그램에서 봇에게 `/start`를 보내면 그 채팅이 관리자
채팅으로 바인딩된다 (다른 사람은 명령 불가).

#### 🔒 시크릿(API 키/토큰)은 파일 대신 환경변수 사용을 권장

`config.json`은 `.gitignore`에 등록돼 있어 git에는 절대 올라가지 않지만,
디스크에는 평문으로 남는다. 더 안전하게 하려면 `config.json`의 해당 값을
비워두고, 아래 환경변수로만 주입하면 된다 (환경변수가 있으면 파일 값보다
항상 우선 적용된다):

- `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

Windows에서 한 번만 영구 등록하려면(사용자 계정 범위, 재부팅/새 터미널부터
적용):

```powershell
setx TELEGRAM_BOT_TOKEN "여기에_토큰"
setx BINANCE_API_KEY "여기에_키"
setx BINANCE_API_SECRET "여기에_시크릿"
```

`setx`는 이미 열려 있는 터미널/실행 중인 프로그램에는 즉시 반영되지 않는다
— 새 터미널을 열거나 `run_all.py`를 다시 실행할 때부터 적용된다.

### 텔레그램 명령

- `/status` — 모드/현재가/상태머신/보유 포지션/잔고 요약
- `/pnl`, `/trades [n]` — 청산 통계 / 최근 트레이드
- `/params` — 현재 전략 파라미터 (A/B 전체)
- `/set a vol_multiplier 2.5` — **파라미터 즉시 변경** (재시작에도 유지, `bot_state.json`에 저장)
- `/pause` / `/resume` — 신규 진입 중지/재개 (청산은 계속 관리됨)
- `/close all` — 보유 중인 (가상) 포지션 전량 청산

### 매매 빈도 관련

트리거 기본값이 v1 사전등록 값보다 **완화**되어 있다 (더 자주 발동):
Setup A `vol_multiplier` 8→3, `min_chain` 3→2, `min_move_pct` 0.8%→0.4%,
`exhaustion_gap_s` 90→45초, `rebound_pct` 0.15%→0.08%, `rebound_hold_s` 30→15초 /
Setup B `oi_increase_pct` 1.5%→0.6%, `retest_window_s` 15분→30분.
너무 잦거나 뜸하면 `/set` 명령으로 언제든 조정하면 된다.

> ⚠️ 추후 실주문을 재활성화할 경우 원금 손실 위험이 있다. 반드시
> 페이퍼 → 테스트넷 → 소액 순서로 검증할 것.

---

- **Setup A** — 청산 캐스케이드 소진 롱 (`liquidation_strategy/setup_a.py`)
- **Setup B** — 트랩드롱 플러시 숏 (`liquidation_strategy/setup_b.py`)
- **엔진** — 두 셋업의 상호배제/TP2 동적전환 규칙 (`liquidation_strategy/engine.py`)
- **임계값 캘리브레이션** — 그리드서치로 트리거 감지 임계값만 탐색 (진입/청산 구조는 고정) (`liquidation_strategy/optimize.py`)
- **합성 데이터** — 실 스트림 연결 전 파이프라인 검증용 (`liquidation_strategy/simulate_data.py`)
- **실데이터 REST 클라이언트** — Binance 공개 REST 래퍼 (`liquidation_strategy/binance_client.py`)
- **실시간 섀도 러너** — forceOrder/aggTrade/kline 웹소켓 + OI 폴링 → 엔진 → JSONL 로그 (`liquidation_strategy/live_feed.py`)
- **Setup B 실데이터 백필** — 과거 klines+OI로 Setup B만 실데이터 백테스트 (`liquidation_strategy/backfill.py`)
- **결과 → 대시보드 JSON 스키마 공용 변환** (`liquidation_strategy/report.py`)
- **Setup C** — OU 평균회귀(박스권 전용, 비유동성 전략). 사전등록:
  `uploads/strategy_c_ou_reversion_spec.md`. Setup A/B(StrategyEngine, 실시간
  스트리밍 상태머신)와 달리 5분봉 DataFrame을 입력받는 순수 함수 + 독립
  원장(`ledger_c_long`/`ledger_c_short`) 구조라 `StrategyEngine`에는 여전히
  연결하지 않았지만, **GUI(Dashboard/Settings/PnL/Log)와 라이브·합성 실행
  경로에는 A/B와 동일한 수준으로 연동돼 있다** — 실주문은 A/B와 똑같이
  비활성(페이퍼)이며, 최소 표본(방향별 30건) 검증 전까지는 `report_c.py`의
  기각조건 판정이 "표본 미도달"로 보류된다(A/B가 라이브 섀도로 표본을
  쌓은 뒤 채택 여부를 정했던 것과 동일한 절차)
  (`liquidation_strategy/setup_c.py`, `live_setup_c.py`, `backtest_c.py`,
  `report_c.py`).
  ```bash
  # 독립 백테스트(과거 데이터로 미리 검증)
  python3 -m liquidation_strategy.backtest_c --days 60 --compare-toggles
  # 라이브/합성 실행은 run_all.py 하나로 A/B/C 전부 동시에 돈다
  ```

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
