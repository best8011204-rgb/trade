"""텔레그램 봇 — 현황 조회 + 전략 파라미터 실시간 수정.

표준 requests 만 사용하는 long-polling 구현 (외부 텔레그램 SDK 불필요).

- 수신(getUpdates)은 asyncio 이벤트 루프 안에서 run_in_executor 로 돌려,
  명령 처리가 스트림 핸들러와 같은 스레드(루프)에서 실행되게 한다 —
  엔진 상태를 잠금 없이 안전하게 읽고/쓸 수 있는 이유.
- 발신(sendMessage)은 전용 데몬 스레드 + 큐로 처리해서, 엔진 콜백에서
  notify() 를 불러도 이벤트 루프가 HTTP 대기로 막히지 않는다.

보안: config 의 telegram_chat_id 와 일치하는 채팅만 응답한다. 비어 있으면
최초로 /start 를 보낸 사용자를 자동으로 바인딩하고 상태 파일에 저장한다.
"""

import asyncio
import dataclasses
import json
import os
import queue
import sys
import threading

import requests

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # 텔레그램 한도 4096 여유분


class BotState:
    """chat_id / paused / 파라미터 오버라이드를 재시작 간 유지."""

    def __init__(self, path="bot_state.json"):
        self.path = path
        self.data = {"chat_id": None, "paused": False, "params": {"a": {}, "b": {}}}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data.update(json.load(f))
            except Exception as e:
                print(f"[state] 로드 실패({e}) — 기본값 사용", file=sys.stderr)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def apply_param_overrides(self, engine):
        """저장된 오버라이드를 엔진 파라미터에 적용 (시작 시 호출)."""
        for key, params_obj in (("a", engine.a.p), ("b", engine.b.p)):
            for name, val in self.data.get("params", {}).get(key, {}).items():
                if hasattr(params_obj, name):
                    setattr(params_obj, name, val)


class TelegramBot:
    def __init__(self, token: str, state: BotState):
        self.token = token
        self.state = state
        self._out_q = queue.Queue()
        self._sender = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender.start()

    # ---- 발신 -----------------------------------------------------------
    def send(self, text: str, chat_id=None):
        chat_id = chat_id or self.state.data.get("chat_id")
        if not chat_id:
            return  # 아직 바인딩된 채팅 없음
        for i in range(0, len(text), MAX_LEN):
            self._out_q.put((chat_id, text[i:i + MAX_LEN]))

    def _sender_loop(self):
        while True:
            chat_id, text = self._out_q.get()
            for attempt in range(3):
                try:
                    r = requests.post(
                        API.format(token=self.token, method="sendMessage"),
                        json={"chat_id": chat_id, "text": text},
                        timeout=15,
                    )
                    if r.status_code == 429:
                        retry = r.json().get("parameters", {}).get("retry_after", 3)
                        threading.Event().wait(retry)
                        continue
                    break
                except Exception as e:
                    print(f"[tg-send] {e} (재시도 {attempt+1}/3)", file=sys.stderr)
                    threading.Event().wait(2 * (attempt + 1))

    # ---- 수신 -----------------------------------------------------------
    async def poll_loop(self, handler):
        """getUpdates long-polling. handler(text) -> 응답 문자열|None."""
        loop = asyncio.get_running_loop()
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
                if offset is not None:
                    params["offset"] = offset
                resp = await loop.run_in_executor(
                    None,
                    lambda p=params: requests.get(
                        API.format(token=self.token, method="getUpdates"),
                        params=p, timeout=60,
                    ),
                )
                data = resp.json()
                if not data.get("ok"):
                    print(f"[tg-poll] API 오류: {data}", file=sys.stderr)
                    await asyncio.sleep(5)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message") or {}
                    text = (msg.get("text") or "").strip()
                    chat_id = msg.get("chat", {}).get("id")
                    if not text or chat_id is None:
                        continue
                    reply = self._authorize_and_handle(chat_id, text, handler)
                    if reply:
                        self.send(reply, chat_id=chat_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[tg-poll] {e}", file=sys.stderr)
                await asyncio.sleep(5)

    def _authorize_and_handle(self, chat_id, text, handler):
        bound = self.state.data.get("chat_id")
        if bound is None:
            if text.startswith("/start"):
                self.state.data["chat_id"] = chat_id
                self.state.save()
                return (f"✅ 이 채팅({chat_id})을 봇 관리자에 바인딩했습니다.\n"
                        "/help 로 명령 목록을 확인하세요.")
            return "먼저 /start 로 이 채팅을 바인딩하세요."
        if chat_id != bound:
            return None  # 권한 없는 채팅은 무시
        return handler(text)


HELP = """사용 가능한 명령:
/status — 봇/포지션/전략 상태 요약
/pnl — 셋업별 청산 통계
/trades [n] — 최근 청산 트레이드 n건 (기본 5)
/params — 현재 전략 파라미터 (A/B)
/set <a|b> <이름> <값> — 파라미터 변경 (즉시 적용+저장)
   예: /set a vol_multiplier 2.5
/pause — 신규 진입 중지 (기존 포지션 청산은 계속)
/resume — 신규 진입 재개
/close all — 실포지션 전량 시장가 청산
/help — 이 도움말"""


class CommandHandler:
    """텔레그램 명령 -> 러너/엔진 조작. poll_loop 와 같은 이벤트 루프에서 실행됨."""

    def __init__(self, runner, state: BotState):
        self.runner = runner
        self.state = state

    def handle(self, text: str) -> str:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        try:
            if cmd in ("/start", "/help"):
                return HELP
            if cmd == "/status":
                return self.runner.status_summary()
            if cmd == "/pnl":
                return self._pnl()
            if cmd == "/trades":
                n = int(args[0]) if args else 5
                return self._trades(n)
            if cmd == "/params":
                return self._params()
            if cmd == "/set":
                return self._set(args)
            if cmd == "/pause":
                self.runner.paused = True
                self.state.data["paused"] = True
                self.state.save()
                return "⏸ 신규 진입을 중지했습니다. (/resume 으로 재개)"
            if cmd == "/resume":
                self.runner.paused = False
                self.state.data["paused"] = False
                self.state.save()
                return "▶️ 신규 진입을 재개했습니다."
            if cmd == "/close":
                if args and args[0].lower() == "all":
                    self.runner.manual_close_all()
                    return "✅ 전량 청산 주문을 보냈습니다. /status 로 확인하세요."
                return "사용법: /close all"
            return "알 수 없는 명령입니다. /help 를 확인하세요."
        except Exception as e:
            return f"⚠️ 명령 처리 오류: {e}"

    # ---- 세부 구현 -------------------------------------------------------
    def _pnl(self):
        s = self.runner.engine.summary()
        if not s:
            return "청산된 트레이드가 아직 없습니다."
        lines = []
        for setup in ("A", "B"):
            st = s.get(setup) or {}
            if not st.get("count"):
                lines.append(f"Setup {setup}: 0건")
                continue
            lines.append(
                f"Setup {setup}: {st['count']}건 · 승률 {st['win_rate']*100:.1f}% · "
                f"평균 {st['avg_bps']:+.1f}bps · 평균R {st['avg_r']:+.2f} · "
                f"최대연속손실 {st['max_consec_losses']}회"
            )
        return "\n".join(lines)

    def _trades(self, n):
        trades = self.runner.engine.closed_trades[-n:]
        if not trades:
            return "청산된 트레이드가 아직 없습니다."
        lines = []
        for t in trades:
            lines.append(f"{t.tag} {t.side.value} {t.reason} | 진입 {t.entry_price:,.1f} → "
                         f"청산 {t.exit_price:,.1f} | {t.bps:+.1f}bps")
        return "\n".join(lines)

    def _params(self):
        out = []
        for key, p in (("a", self.runner.engine.a.p), ("b", self.runner.engine.b.p)):
            out.append(f"— Setup {key.upper()} —")
            for f in dataclasses.fields(p):
                mark = " *" if f.name in self.state.data["params"].get(key, {}) else ""
                out.append(f"{f.name} = {getattr(p, f.name)}{mark}")
        out.append("(* = 텔레그램에서 수정됨)")
        return "\n".join(out)

    def _set(self, args):
        if len(args) != 3:
            return "사용법: /set <a|b> <파라미터명> <값>\n예: /set a vol_multiplier 2.5"
        setup, name, raw = args[0].lower(), args[1], args[2]
        if setup not in ("a", "b"):
            return "첫 인자는 a 또는 b 여야 합니다."
        p = self.runner.engine.a.p if setup == "a" else self.runner.engine.b.p
        field_map = {f.name: f for f in dataclasses.fields(p)}
        if name not in field_map:
            return f"'{name}' 파라미터가 없습니다. /params 로 이름을 확인하세요."
        old = getattr(p, name)
        try:
            val = int(raw) if isinstance(old, int) and not isinstance(old, bool) else float(raw)
        except ValueError:
            return f"값 '{raw}' 을 숫자로 변환할 수 없습니다."
        setattr(p, name, val)
        self.state.data["params"].setdefault(setup, {})[name] = val
        self.state.save()
        return f"✅ Setup {setup.upper()} {name}: {old} → {val} (즉시 적용, 재시작에도 유지)"
