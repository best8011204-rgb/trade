"""Trade Engine GUI — liquidation_strategy 엔진을 표시/제어하는 tkinter 프론트엔드.

이 패키지는 liquidation_strategy(엔진)를 import만 하며, 엔진 쪽은 이 패키지를
전혀 모른다. GUI는 controllers를 통해서만 엔진을 시작/정지하고, 엔진의 상태는
event_bus를 거쳐 단방향으로 GUI에 전달된다.
"""
