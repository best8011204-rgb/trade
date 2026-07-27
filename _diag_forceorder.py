"""진단용 1회성 스크립트 — Binance 청산(forceOrder) 스트림이 실제로 오는지 직접 확인.
run_all.py와 무관하게 독립적으로 실행. 터미널에서 직접:
    python _diag_forceorder.py                              # 프록시 없이 (직결)
    python _diag_forceorder.py socks5://127.0.0.1:1080       # 프록시 경유 테스트
2분간 리스닝 후 자동 종료. 카운트와 첫 event 몇 개를 출력한다.
"""
import asyncio
import json
import sys
import time

import websockets

STREAM_URL = "wss://fstream.binance.com/stream?streams={streams}"
DURATION_S = 120


async def inner(proxy):
    streams = "/".join(["btcusdt@aggTrade", "btcusdt@forceOrder", "!forceOrder@arr"])
    url = STREAM_URL.format(streams=streams)
    print(f"[connecting] {url}  (proxy={proxy!r})")
    counts = {}
    t0 = time.time()
    async with websockets.connect(url, proxy=proxy, ping_interval=180, ping_timeout=60) as ws:
        print("[connected] handshake ok, listening...")
        async for raw in ws:
            msg = json.loads(raw)
            stream = msg.get("stream", "")
            counts[stream] = counts.get(stream, 0) + 1
            total = sum(counts.values())
            if total <= 5 or stream == "btcusdt@forceOrder":
                print(f"[{time.time()-t0:6.1f}s] {stream}: {json.dumps(msg['data'])[:200]}")


async def main():
    proxy = sys.argv[1] if len(sys.argv) > 1 else True
    try:
        await asyncio.wait_for(inner(proxy), timeout=DURATION_S)
    except asyncio.TimeoutError:
        pass
    print(f"\n=== {DURATION_S}s 종료 ===")


if __name__ == "__main__":
    asyncio.run(main())
