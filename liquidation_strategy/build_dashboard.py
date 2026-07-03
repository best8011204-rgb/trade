"""liquidation_strategy_output.json -> 자기완결 HTML 대시보드로 변환."""

import json
import sys

TEMPLATE_PATH = "liquidation_strategy/dashboard_template.html"
DATA_PATH = "liquidation_strategy_output.json"
OUT_PATH = "liquidation_strategy_dashboard.html"


def main():
    with open(DATA_PATH) as f:
        data = json.load(f)
    with open(TEMPLATE_PATH) as f:
        tmpl = f.read()

    payload = json.dumps(data, ensure_ascii=False)
    out = tmpl.replace("__DATA_JSON__", payload)

    with open(OUT_PATH, "w") as f:
        f.write(out)
    print(f"wrote {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
