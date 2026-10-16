"""Command-line tournament run:  uv run python -m app.validation.run_cli [--force] [--only id1,id2]

Writes the report to var/tournament/<run_id>/ (JSON, CSV, HTML); the dashboard's Strategy Lab reads it.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore a cached result for identical data + config")
    ap.add_argument("--only", default="", help="comma-separated strategy ids")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    from . import tournament as tn

    only = [x.strip() for x in args.only.split(",") if x.strip()] or None
    done = threading.Event()

    def ticker() -> None:
        while not done.wait(30):
            st = tn.status()
            print(f"[{time.strftime('%H:%M:%S')}] {st['progress']}/{st['total']} {st.get('message')} "
                  f"running: {', '.join(st.get('current', [])[:6])}", flush=True)

    threading.Thread(target=ticker, daemon=True).start()
    t0 = time.time()
    with tn._lock:
        tn._state["state"] = "running"
    rep = tn.run_tournament(args.force, only)
    with tn._lock:
        tn._state.update({"state": "done", "run_id": rep["run_id"]})
    done.set()
    print(f"\n{rep['headline']}  ({time.time() - t0:.0f} s, run {rep['run_id']})")
    for r in rep["leaderboard"]:
        print(f"{r['no']:>2} {r['id']:22} {r['result']:7} {str(r['first_failed']):24} trades={r['oos_trades']} "
              f"PF={r['profit_factor']} exp={r['expectancy_r']} net={r['net_return_pct']} dd={r['max_drawdown_pct']}")
    if rep.get("control"):
        print("control:", rep["control"]["passed"], "of", rep["control"]["tested"], "passed")
    for s in rep["summary"]:
        print("-", s)


if __name__ == "__main__":
    main()
