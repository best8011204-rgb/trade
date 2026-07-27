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
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # 텔레그램 한도 4096 여유분


def _serialize_ab_legs(open_legs) -> list:
    """Setup A/B의 OpenLeg -> Setup C(open_legs_view())와 동일한 dict 형태.
    /status가 A/B/C 보유 포지션을 한 목록으로 합쳐 보여주기 위한 용도."""
    out = []
    for leg in open_legs:
        t = leg.trade
        out.append({
            "setup": t.setup, "side": t.side.value, "entry_price": t.entry_price,
            "sl": leg.sl, "tp1": leg.tp1, "tp2": leg.tp2, "tag": t.tag,
        })
    return out


class BotState:
    """chat_id / paused / 파라미터 오버라이드를 재시작 간 유지."""

    def __init__(self, path="bot_state.json"):
        self.path = path
        self.data = {"chat_id": None, "paused": False, "params": {"a": {}, "b": {}, "c": {}}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception as e:
                print(f"[state] 로드 실패({e}) — 기본값 사용", file=sys.stderr)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def apply_param_overrides(self, engine, c_runner=None):
        """저장된 오버라이드를 엔진(A/B)과 Setup C 파라미터에 적용 (시작 시 호출).
        c_runner는 헤드리스(run_bot.py)/GUI(run_all.py) 둘 다 항상 있지만,
        과거 호출부와의 호환을 위해 선택 인자로 둔다 — 없으면 "c" 오버라이드는
        건너뛴다."""
        targets = [("a", engine.a.p), ("b", engine.b.p)]
        if c_runner is not None:
            targets.append(("c", c_runner.p))
        for key, params_obj in targets:
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
            self._out_q.put(("text", chat_id, text[i:i + MAX_LEN], None))

    def send_photo(self, image_bytes: bytes, chat_id=None, caption: str = None):
        """GUI 스크린샷 등 이미지 전송(/status). caption은 sendPhoto 자체 한도(1024자)를
        넘지 않아야 한다 — 호출부(CommandHandler)가 그 전에 길이를 맞춘다."""
        chat_id = chat_id or self.state.data.get("chat_id")
        if not chat_id:
            return
        self._out_q.put(("photo", chat_id, image_bytes, caption))

    def _sender_loop(self):
        while True:
            kind, chat_id, payload, extra = self._out_q.get()
            for attempt in range(3):
                try:
                    if kind == "text":
                        r = requests.post(
                            API.format(token=self.token, method="sendMessage"),
                            json={"chat_id": chat_id, "text": payload},
                            timeout=15,
                        )
                    else:  # "photo"
                        data = {"chat_id": chat_id}
                        if extra:
                            data["caption"] = extra
                        files = {"photo": ("status.png", payload, "image/png")}
                        r = requests.post(
                            API.format(token=self.token, method="sendPhoto"),
                            data=data, files=files, timeout=30,
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
/status — 봇/포지션/전략 상태 요약 (GUI 실행 중이면 화면 캡처도 함께 전송)
/conditions — 셋업별 트리거 조건 상세 체크리스트
/pnl — 셋업별 청산 통계
/trades [n] — 최근 청산 트레이드 n건 (기본 5)
/params — 현재 전략 파라미터 (A/B/C)
/set <a|b|c> <이름> <값> — 파라미터 1개 변경 (즉시 적용+저장)
   예: /set a price_drop_atr_mult 2.0
/seta <이름1>=<값1> [이름2=값2 ...] — Setup A 파라미터 일괄 변경
/setb <이름1>=<값1> [이름2=값2 ...] — Setup B 파라미터 일괄 변경
/setc <이름1>=<값1> [이름2=값2 ...] — Setup C 파라미터 일괄 변경
   예: /seta lookback_hours=6 min_move_pct=0.004 rebound_hold_s=10
/pause — 신규 진입 중지 (기존 포지션 청산은 계속)
/resume — 신규 진입 재개
/close all — 보유 포지션 전량 청산
/help — 이 도움말"""


class CommandHandler:
    """텔레그램 명령 -> 러너/엔진 조작. poll_loop 와 같은 이벤트 루프에서 실행됨.

    bus(EventBus)를 넘기면 파라미터 변경/일시정지/청산 같은 "적용된 변경"을
    GUI의 Log 탭("config" 토픽)에도 남긴다. GUI 없이 헤드리스로 도는
    run_bot.py 는 bus 없이 생성하면 되고, 이 경우 그냥 조용히 건너뛴다.
    """

    def __init__(self, runner, state: BotState, bus=None, c_runner=None,
                 screenshot_fn=None, send_photo_fn=None):
        self.runner = runner
        self.state = state
        self.bus = bus
        self.c_runner = c_runner  # Setup C(C1+C2) 러너 — GUI(run_all.py)/헤드리스(run_bot.py) 둘 다 있음.
        self.screenshot_fn = screenshot_fn      # GUI 창 캡처(PNG bytes 반환) — GUI(run_all.py) 경로에서만 있음.
        self.send_photo_fn = send_photo_fn      # TelegramBot.send_photo — 위와 동일 조건.

    def _log(self, msg: str):
        if self.bus is not None:
            self.bus.publish("config", {"ts": time.time(), "msg": f"[텔레그램] {msg}"})

    def handle(self, text: str) -> str:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        try:
            if cmd in ("/start", "/help"):
                return HELP
            if cmd == "/status":
                return self._status(args)
            if cmd == "/conditions":
                return self._conditions_text()
            if cmd == "/pnl":
                return self._pnl()
            if cmd == "/trades":
                n = int(args[0]) if args else 5
                return self._trades(n)
            if cmd == "/params":
                return self._params()
            if cmd == "/set":
                return self._set(args)
            if cmd in ("/seta", "/setb", "/setc"):
                return self._set_bulk(cmd[-1], args)
            if cmd == "/pause":
                self.runner.paused = True
                self.state.data["paused"] = True
                self.state.save()
                self._log("신규 진입 일시정지 (/pause)")
                return "⏸ 신규 진입을 중지했습니다. (/resume 으로 재개)"
            if cmd == "/resume":
                self.runner.paused = False
                self.state.data["paused"] = False
                self.state.save()
                self._log("신규 진입 재개 (/resume)")
                return "▶️ 신규 진입을 재개했습니다."
            if cmd == "/close":
                if args and args[0].lower() == "all":
                    self.runner.manual_close_all()
                    self._log("포지션 전량 청산 요청 (/close all)")
                    return "✅ 전량 청산 주문을 보냈습니다. /status 로 확인하세요."
                return "사용법: /close all"
            return "알 수 없는 명령입니다. /help 를 확인하세요."
        except Exception as e:
            return f"⚠️ 명령 처리 오류: {e}"

    # ---- 세부 구현 -------------------------------------------------------
    def _status(self, args) -> str:
        """/status — 간결하고 통일된 한 화면 요약. GUI(run_all.py)가 화면
        캡처를 지원하면(screenshot_fn/send_photo_fn 둘 다 있으면) 이 요약을
        사진 캡션으로 붙여 화면 캡처와 함께 보낸다(텍스트 메시지는 따로 안
        보냄 — poll_loop가 None 반환을 "이미 보냈으니 추가 전송 없음"으로
        처리한다). 헤드리스(run_bot.py)거나 캡처에 실패하면 텍스트만 보낸다.
        조건 체크리스트(T1~T4 등 상세)는 길어서 /conditions로 분리했다."""
        text = self._status_text()
        if self.screenshot_fn is not None and self.send_photo_fn is not None:
            try:
                img_bytes = self.screenshot_fn()
            except Exception as e:
                return text + f"\n\n⚠️ 화면 캡처 실패: {e}"
            # sendPhoto의 caption 한도(1024자)를 넘으면 캡션 없이 사진만 보내고
            # 텍스트는 별도 메시지로 정상 반환한다.
            if len(text) <= 1000:
                self.send_photo_fn(img_bytes, caption=text)
                return None
            self.send_photo_fn(img_bytes)
            return text
        return text

    def _status_text(self) -> str:
        r = self.runner
        eng = r.engine
        up_h = (time.time() - r.started_at) / 3600
        mode = "실거래" if r.trader else "페이퍼(주문 없음)"
        if r.trader and r.trader.testnet:
            mode += " · 테스트넷"
        if r.paused:
            mode += " · ⏸ 일시정지"
        price_txt = f"{r.last_price:,.1f}" if r.last_price else "수신 대기"

        states = [f"A={eng.a.state}", f"B={eng.b.state}"]
        if self.c_runner is not None:
            states.append(f"C1={self.c_runner.c1_state.state}")
            states.append(f"C2={self.c_runner.c2_state.state}")

        lines = [
            f"🤖 {r.symbol.upper()} · {mode} · 가동 {up_h:.1f}h",
            f"현재가: {price_txt}",
            "",
            "[상태] " + " · ".join(states),
        ]

        legs = _serialize_ab_legs(eng.open_legs)
        if self.c_runner is not None:
            legs += self.c_runner.open_legs_view()
        lines.append("")
        lines.append(f"[보유 포지션] {len(legs)}개")
        if not legs:
            lines.append("  없음")
        for leg in legs:
            tp = (f" / TP1 {leg['tp1']:,.1f} / TP2 {leg['tp2']:,.1f}"
                  if leg.get("tp1") is not None else "")
            lines.append(f"  {leg['tag']} {leg['side']} @ {leg['entry_price']:,.1f} "
                         f"(SL {leg['sl']:,.1f}{tp})")

        lines.append("")
        lines.append("[누적 성과]")
        s = eng.summary()
        perf_rows = []
        for setup in ("A", "B"):
            st = s.get(setup) or {}
            perf_rows.append((setup, st))
        if self.c_runner is not None:
            cs = self.c_runner.summary()
            for setup in ("C1", "C2"):
                perf_rows.append((setup, cs.get(setup) or {}))
        for setup, st in perf_rows:
            if st.get("count"):
                lines.append(f"  {setup:<2} {st['count']:>3}건 · 승률 {st['win_rate']*100:4.1f}% · "
                             f"평균 {st['avg_bps']:+.1f}bps")
            else:
                lines.append(f"  {setup:<2}   0건")

        if r.trader:
            try:
                pos = r.trader.get_position()
                bal = r.trader.get_usdt_balance()
                lines.append("")
                lines.append(f"거래소 포지션: {pos['qty']:+g} (미실현 {pos['unrealized']:+.2f} USDT)")
                lines.append(f"가용 잔고: {bal:,.2f} USDT")
            except Exception as e:
                lines.append(f"거래소 조회 실패: {e}")

        lines.append("")
        lines.append("조건 상세 -> /conditions")
        return "\n".join(lines)

    def _conditions_text(self) -> str:
        """/conditions — 셋업별 요구조건 체크리스트를 현재 실시간 값과 함께
        보여준다. 가격은 최신 체결틱(self.runner.last_price)을 써서, 예를
        들어 "하락률 >= 0.3%" 같은 조건의 실제 현재 값이 매 호출마다 최신으로
        나온다(봉 확정을 기다리지 않음). RVOL/OI처럼 원본 데이터 자체가
        1분/5분 단위인 조건은 그 데이터의 마지막 확정치를 보여준다."""
        now = time.time()
        price = self.runner.last_price
        oi = getattr(self.runner, "_latest_oi", None)
        lines = ["[조건 상세]"]
        lines.append("Setup A:")
        lines.extend(self._format_conditions(self.runner.engine.a.conditions(now, current_price=price)))
        lines.append("Setup B:")
        lines.extend(self._format_conditions(self.runner.engine.b.conditions(now, current_price=price)))
        if self.c_runner is not None:
            lines.append("Setup C:")
            lines.extend(self._format_conditions(self.c_runner.live_conditions(price, oi)))
        return "\n".join(lines)

    @staticmethod
    def _format_conditions(conds: list) -> list:
        out = []
        for c in conds:
            mark = "✓" if c["met"] else "✗"
            out.append(f"  {mark} {c['label']} — {c['detail']}")
        return out

    def _pnl(self):
        s = self.runner.engine.summary()
        cs = self.c_runner.summary() if self.c_runner is not None else {}
        total = sum((s.get(k) or {}).get("count", 0) for k in ("A", "B"))
        total += sum((cs.get(k) or {}).get("count", 0) for k in ("C1", "C2"))
        if total == 0:
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
        if self.c_runner is not None:
            for setup in ("C1", "C2"):
                st = cs.get(setup) or {}
                if not st.get("count"):
                    lines.append(f"Setup {setup}: 0건")
                    continue
                lines.append(
                    f"Setup {setup}: {st['count']}건 · 승률 {st['win_rate']*100:.1f}% · "
                    f"평균 {st['avg_bps']:+.1f}bps · 평균R {st['avg_r']:+.2f}"
                )
        return "\n".join(lines)

    def _all_closed_trades(self) -> list:
        """엔진(A/B)의 Trade 객체와 Setup C(c_runner)의 dict 트레이드를
        exit_ts 기준으로 합쳐 정렬한 공통 포맷(dict) 리스트로 반환한다 —
        /trades가 셋업 구분 없이 최근 체결을 시간순으로 보여주기 위함."""
        out = []
        for t in self.runner.engine.closed_trades:
            out.append({
                "tag": t.tag, "side": t.side.value, "reason": t.reason,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "exit_ts": t.exit_ts, "bps": t.bps,
            })
        if self.c_runner is not None:
            for t in self.c_runner.closed_trades:
                out.append({
                    "tag": t["tag"], "side": t["side"], "reason": t["reason"],
                    "entry_price": t["entry_price"], "exit_price": t["exit_price"],
                    "exit_ts": t["exit_ts"], "bps": t["bps"],
                })
        out.sort(key=lambda t: t["exit_ts"])
        return out

    def _trades(self, n):
        trades = self._all_closed_trades()[-n:]
        if not trades:
            return "청산된 트레이드가 아직 없습니다."
        lines = []
        for t in trades:
            lines.append(f"{t['tag']} {t['side']} {t['reason']} | 진입 {t['entry_price']:,.1f} → "
                         f"청산 {t['exit_price']:,.1f} | {t['bps']:+.1f}bps")
        return "\n".join(lines)

    def _params_obj(self, setup: str):
        """setup("a"|"b"|"c") -> 해당 파라미터 dataclass 인스턴스, 없으면 None."""
        if setup == "a":
            return self.runner.engine.a.p
        if setup == "b":
            return self.runner.engine.b.p
        if setup == "c" and self.c_runner is not None:
            return self.c_runner.p
        return None

    def _params(self):
        out = []
        targets = [("a", self.runner.engine.a.p), ("b", self.runner.engine.b.p)]
        if self.c_runner is not None:
            targets.append(("c", self.c_runner.p))
        for key, p in targets:
            out.append(f"— Setup {key.upper()} —")
            for f in dataclasses.fields(p):
                mark = " *" if f.name in self.state.data["params"].get(key, {}) else ""
                out.append(f"{f.name} = {getattr(p, f.name)}{mark}")
        out.append("(* = 텔레그램에서 수정됨)")
        return "\n".join(out)

    def _set(self, args):
        if len(args) != 3:
            return "사용법: /set <a|b|c> <파라미터명> <값>\n예: /set a price_drop_atr_mult 2.0"
        setup, name, raw = args[0].lower(), args[1], args[2]
        if setup not in ("a", "b", "c"):
            return "첫 인자는 a, b, c 중 하나여야 합니다."
        p = self._params_obj(setup)
        if p is None:
            return f"Setup {setup.upper()}이(가) 이 프로세스에서 실행 중이지 않습니다."
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
        self._log(f"/set {setup} {name}: {old} → {val}")
        return f"✅ Setup {setup.upper()} {name}: {old} → {val} (즉시 적용, 재시작에도 유지)"

    def _set_bulk(self, setup: str, args) -> str:
        """/seta, /setb, /setc — "이름=값" 토큰 여러 개를 한 번에 적용한다.
        일부만 틀려도 아무것도 적용하지 않는다(전부 검증 통과 후 일괄 반영) —
        절반만 적용된 애매한 상태를 만들지 않기 위함."""
        cmd_name = f"/set{setup}"
        if not args:
            return (f"사용법: {cmd_name} 이름1=값1 이름2=값2 ...\n"
                     f"예: {cmd_name} lookback_hours=6 min_move_pct=0.004")
        p = self._params_obj(setup)
        if p is None:
            return f"Setup {setup.upper()}이(가) 이 프로세스에서 실행 중이지 않습니다."
        field_map = {f.name: f for f in dataclasses.fields(p)}

        updates = []
        for tok in args:
            if "=" not in tok:
                return f"'{tok}' — '이름=값' 형식이어야 합니다 (예: lookback_hours=6)."
            name, raw = tok.split("=", 1)
            if name not in field_map:
                return f"'{name}' 파라미터가 없습니다. /params 로 이름을 확인하세요."
            old = getattr(p, name)
            try:
                val = int(raw) if isinstance(old, int) and not isinstance(old, bool) else float(raw)
            except ValueError:
                return f"값 '{raw}' 을 숫자로 변환할 수 없습니다 ({name})."
            updates.append((name, old, val))

        for name, _old, val in updates:
            setattr(p, name, val)
            self.state.data["params"].setdefault(setup, {})[name] = val
        self.state.save()
        self._log(f"{cmd_name} 일괄 변경: " + ", ".join(f"{n}={v}" for n, _, v in updates))

        lines = [f"✅ Setup {setup.upper()} 일괄 변경(즉시 적용, 재시작에도 유지):"]
        for name, old, val in updates:
            lines.append(f"  {name}: {old} → {val}")
        return "\n".join(lines)
