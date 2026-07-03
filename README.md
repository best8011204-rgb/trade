# BTC 청산 흐름 전략 v1.0 — 알고리즘 구현 + 검증 대시보드

`liquidationstrategyv1preregistration.md` 사전등록 명세를 코드로 옮긴 구현체.

- **Setup A** — 청산 캐스케이드 소진 롱 (`liquidation_strategy/setup_a.py`)
- **Setup B** — 트랩드롱 플러시 숏 (`liquidation_strategy/setup_b.py`)
- **엔진** — 두 셋업의 상호배제/TP2 동적전환 규칙 (`liquidation_strategy/engine.py`)
- **임계값 캘리브레이션** — 그리드서치로 트리거 감지 임계값만 탐색 (진입/청산 구조는 고정) (`liquidation_strategy/optimize.py`)
- **합성 데이터** — 실 스트림 연결 전 파이프라인 검증용 (`liquidation_strategy/simulate_data.py`)
- **백테스트 + 기각조건 판정 + 시각화 JSON 출력** (`liquidation_strategy/run.py`)

## 실행

```bash
pip install -r requirements.txt
python3 -m liquidation_strategy.run                # liquidation_strategy_output.json 생성
python3 -m liquidation_strategy.build_dashboard     # liquidation_strategy_dashboard.html 생성
```

`liquidation_strategy_dashboard.html`을 브라우저로 열면 트리거 로직 다이어그램,
가격/진입 시그널 차트, 누적 기대값, 임계값 캘리브레이션 결과, 기각조건 판정을
확인할 수 있다.

## 실데이터 연동

`simulate_data.py`만 실제 어댑터(Binance `forceOrder`/`openInterest`/`aggTrade`
웹소켓)로 교체하면 나머지 탐지·집행·기각판정 로직은 그대로 재사용된다.
실거래 검증은 명세서 5장 절차(섀도 → 소액 라이브)를 따른다.

## 주의

현재 결과는 실거래소 접속 없이 명세서의 트리거 정의를 모사한 합성 스트레스
시나리오 기반이며, 전략의 실제 수익성을 보장하지 않는다. 대시보드의
기각조건 판정은 이 합성 데이터에 대한 것으로, 실 데이터 섀도 검증을
대체하지 않는다.
