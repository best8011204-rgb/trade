"""GUI 프론트엔드 서버: 파이프라인 산출물(JSON)을 터미널형 대시보드에 실시간 바인딩.

정적 빌드(build_dashboard.py)와 달리, 이 모듈은 매 요청마다
liquidation_strategy_output.json 을 다시 읽어 템플릿에 주입한다.
run.py / run_live.py / backfill.py 가 JSON을 갱신하면 열려 있는
브라우저가 자동으로 리로드된다(mtime 폴링).

실행:
    python3 -m liquidation_strategy.gui                # http://127.0.0.1:8760
    python3 -m liquidation_strategy.gui --port 9000
    python3 -m liquidation_strategy.gui --open         # 브라우저 자동 오픈
    python3 -m liquidation_strategy.gui --data path/to/output.json

의존성: 표준 라이브러리만 사용.
"""

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = PKG_DIR / "dashboard_template.html"
DEFAULT_DATA_PATH = PKG_DIR.parent / "liquidation_strategy_output.json"

# JSON 변경 감지 시 페이지를 리로드하는 폴링 스크립트 (서버 모드에서만 주입)
AUTO_RELOAD_SNIPPET = """
<script>
(function () {
  let last = null;
  async function poll() {
    try {
      const r = await fetch("/api/mtime", { cache: "no-store" });
      const j = await r.json();
      if (last !== null && j.mtime !== last) location.reload();
      last = j.mtime;
    } catch (e) { /* 서버 재시작 중 등 — 무시하고 계속 폴링 */ }
    setTimeout(poll, 4000);
  }
  poll();
})();
</script>
"""


class DashboardState:
    """템플릿/데이터 로딩과 캐시. 데이터 파일은 mtime 기준으로 재로딩."""

    def __init__(self, data_path: Path, template_path: Path = TEMPLATE_PATH):
        self.data_path = data_path
        self.template_path = template_path
        self._cache_mtime = None
        self._cache_html = None
        self._lock = threading.Lock()

    def data_mtime(self):
        try:
            return self.data_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def render(self) -> bytes:
        mtime = self.data_mtime()
        with self._lock:
            if mtime is not None and mtime == self._cache_mtime and self._cache_html:
                return self._cache_html
            template = self.template_path.read_text(encoding="utf-8")
            if mtime is None:
                data_json = json.dumps({
                    "meta": {"data_source": "missing"},
                    "optimized_params": {"A": {}, "B": {}},
                    "grid_search": {"A": [], "B": []},
                    "summary": {"A": {}, "B": {}},
                    "rejection": {
                        "A": {"rejected": False, "checks": []},
                        "B": {"rejected": False, "checks": []},
                    },
                    "trades": [], "equity_curve": [], "price_overview": [],
                    "sample_window_a": [], "sample_window_b": [],
                    "sample_trade_a": None, "sample_trade_b": None,
                }, ensure_ascii=False)
                banner = (
                    "<div style='position:fixed;inset:auto 0 40px 0;text-align:center;z-index:99'>"
                    "<span style='background:#F6465A;color:#fff;padding:6px 14px;border-radius:6px;"
                    "font-size:12px'>산출물 없음 — python3 -m liquidation_strategy.run 을 먼저 실행</span></div>"
                )
            else:
                data_json = self.data_path.read_text(encoding="utf-8")
                banner = ""
            html = template.replace("__DATA_JSON__", data_json) + banner + AUTO_RELOAD_SNIPPET
            self._cache_mtime = mtime
            self._cache_html = html.encode("utf-8")
            return self._cache_html


def make_handler(state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/dashboard"):
                self._send(200, state.render())
            elif path == "/api/data":
                mtime = state.data_mtime()
                if mtime is None:
                    self._send(404, b'{"error":"output json not found"}',
                               "application/json; charset=utf-8")
                else:
                    self._send(200, state.data_path.read_bytes(),
                               "application/json; charset=utf-8")
            elif path == "/api/mtime":
                body = json.dumps({"mtime": state.data_mtime()}).encode()
                self._send(200, body, "application/json; charset=utf-8")
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):  # 조용한 로그
            sys.stderr.write("[gui] %s - %s\n" % (self.address_string(), fmt % args))

    return Handler


def serve(host="127.0.0.1", port=8760, data_path=DEFAULT_DATA_PATH, open_browser=False):
    state = DashboardState(Path(data_path))
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    url = f"http://{host}:{port}/"
    print(f"[gui] 대시보드 서버 시작: {url}", file=sys.stderr)
    print(f"[gui] 데이터: {state.data_path} "
          f"({'존재' if state.data_mtime() else '없음 — run.py 실행 필요'})", file=sys.stderr)
    print("[gui] JSON 갱신 시 브라우저 자동 리로드. Ctrl+C 로 종료.", file=sys.stderr)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[gui] 종료", file=sys.stderr)
    finally:
        httpd.server_close()


def main():
    ap = argparse.ArgumentParser(description="청산 흐름 전략 GUI 서버")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--data", default=str(DEFAULT_DATA_PATH),
                    help="파이프라인 산출물 JSON 경로")
    ap.add_argument("--open", action="store_true", help="브라우저 자동 오픈")
    args = ap.parse_args()
    serve(host=args.host, port=args.port, data_path=args.data, open_browser=args.open)


if __name__ == "__main__":
    main()
