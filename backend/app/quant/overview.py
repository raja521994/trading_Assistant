"""Market overview aggregation (indices, breadth, sectors, leaders, regime, feed health)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from ..data.repository import list_instruments
from ..providers.registry import active_provider, provider_fallback_reason
from ..providers.sample import market_session_state, now_ist
from .regime import classify_regime
from .universe import features, instrument_map, last_row, load_benchmark, load_universe

OVERVIEW_INDICES = ["NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "SENSEX"]


def _chg(df: pd.DataFrame, n: int) -> float | None:
    if len(df) <= n:
        return None
    return round(float(df["close"].iloc[-1] / df["close"].iloc[-1 - n] - 1.0) * 100, 3)


def market_overview(session: Session) -> dict[str, Any]:
    provider = active_provider()
    now = now_ist()
    label = provider.capabilities().data_status.value
    imap = instrument_map(session)
    daily = load_universe(session, "1d")
    idx_daily = load_universe(session, "1d", "INDEX")

    def live(exch: str, sym: str, df: pd.DataFrame) -> dict[str, Any]:
        q = provider.quote(sym, exch)
        if q is not None:
            return {"last": round(q.last_price, 2), "prev_close": round(q.prev_close, 2),
                    "change": round(q.last_price - q.prev_close, 2), "change_pct": round(q.change_pct, 3),
                    "ts": q.ts.isoformat(), "market_open": q.market_open}
        c, p = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
        return {"last": c, "prev_close": p, "change": round(c - p, 2), "change_pct": round((c / p - 1) * 100, 3),
                "ts": df.index[-1].isoformat(), "market_open": False}

    indices = []
    for (exch, sym), df in sorted(idx_daily.items(), key=lambda kv: (kv[0][1] not in OVERVIEW_INDICES,
                                                                     OVERVIEW_INDICES.index(kv[0][1])
                                                                     if kv[0][1] in OVERVIEW_INDICES else 99,
                                                                     kv[0][1])):
        if len(df) < 2:
            continue
        indices.append({"symbol": sym, "exchange": exch, "name": imap[(exch, sym)].name, **live(exch, sym, df),
                        "change_1w_pct": _chg(df, 5), "change_1m_pct": _chg(df, 21),
                        "spark": [round(float(x), 2) for x in df["close"].iloc[-30:]],
                        "headline": sym in OVERVIEW_INDICES, "data_label": label})

    rows = []
    for (exch, sym), df in daily.items():
        if len(df) < 25:
            continue
        inst = imap[(exch, sym)]
        lv = live(exch, sym, df)
        f = last_row(features((exch, sym), df))
        rows.append({"symbol": sym, "exchange": exch, "name": inst.name, "sector": inst.sector, **lv,
                     "change_1w_pct": _chg(df, 5), "change_1m_pct": _chg(df, 21),
                     "volume": float(df["volume"].iloc[-1]), "vol_ratio": f.get("vol_ratio"),
                     "atr_pct": f.get("atr_pct"), "turnover_cr": (f.get("turnover20") or 0) / 1e7})
    adv = sum(1 for r in rows if r["change_pct"] > 0)
    dec = sum(1 for r in rows if r["change_pct"] < 0)
    unch = len(rows) - adv - dec
    sectors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        sectors[r["sector"] or "Other"].append(r)

    def avg(xs: list[float | None]) -> float | None:
        v = [x for x in xs if x is not None]
        return round(sum(v) / len(v), 3) if v else None

    heat = sorted([{"sector": s, "count": len(rs), "change_pct": avg([r["change_pct"] for r in rs]),
                    "change_1w_pct": avg([r["change_1w_pct"] for r in rs]),
                    "change_1m_pct": avg([r["change_1m_pct"] for r in rs]),
                    "advances": sum(1 for r in rs if r["change_pct"] > 0)} for s, rs in sectors.items()],
                  key=lambda x: -(x["change_pct"] or 0))
    by_chg = sorted(rows, key=lambda r: r["change_pct"], reverse=True)
    bench = load_benchmark(session)
    regime = classify_regime(bench, daily) if bench is not None else None
    health = provider.health()
    exp_1d = provider.expected_last_bar("1d", now)
    last_daily = max((df.index[-1] for df in daily.values()), default=None)
    return {
        "generated_at": now.isoformat(), "data_label": label, "provider": provider.name,
        "provider_note": provider_fallback_reason(), "market_session": market_session_state(now),
        "indices": indices,
        "breadth": {"advances": adv, "declines": dec, "unchanged": unch, "total": len(rows),
                    "ad_ratio": round(adv / dec, 2) if dec else None},
        "sectors": heat,
        "gainers": by_chg[:6], "losers": list(reversed(by_chg[-6:])),
        "volume_leaders": sorted([r for r in rows if r["vol_ratio"]], key=lambda r: -r["vol_ratio"])[:6],
        "volatility_leaders": sorted([r for r in rows if r["atr_pct"]], key=lambda r: -r["atr_pct"])[:6],
        "regime": regime,
        "announcements": {"status": "NOT CONNECTED",
                          "message": "No authorised news / exchange-announcement provider is configured."},
        "feed": {"status": health.status.value, "connected": health.connected, "message": health.message,
                 "latency_ms": health.latency_ms,
                 "last_daily_bar": last_daily.isoformat() if last_daily is not None else None,
                 "expected_daily_bar": exp_1d.isoformat() if exp_1d else None,
                 "instruments": len(list_instruments(session))},
    }
