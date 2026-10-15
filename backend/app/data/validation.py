"""Data-quality validation: OHLC consistency, duplicates, gaps, staleness, sufficiency."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

BARS_PER_SESSION = 75


@dataclass
class DataQuality:
    ok: bool
    status: str  # OK | EMPTY | INVALID | STALE | GAPS | INSUFFICIENT
    freshness: str  # FRESH | STALE | UNKNOWN
    bars: int
    last_ts: str | None
    expected_last_ts: str | None
    missing_bars: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_candles(df: pd.DataFrame, timeframe: str, expected_last: datetime | None, now: datetime | None = None,
                     min_bars: int = 60, window: int = 250) -> DataQuality:
    if df is None or df.empty:
        return DataQuality(False, "EMPTY", "UNKNOWN", 0, None, _iso(expected_last), issues=["No candles available"])
    issues: list[str] = []
    recent = df.iloc[-window:]
    last_ts = df.index[-1]

    bad = (recent["high"] < recent[["open", "close"]].max(axis=1)) | \
          (recent["low"] > recent[["open", "close"]].min(axis=1)) | (recent["low"] <= 0)
    n_bad = int(bad.sum())
    if n_bad:
        issues.append(f"{n_bad} bar(s) with inconsistent OHLC values in the last {len(recent)} bars")
    dups = int(recent.index.duplicated().sum())
    if dups:
        issues.append(f"{dups} duplicate timestamp(s)")

    missing = 0
    if timeframe == "1d":
        expected_days = pd.bdate_range(recent.index[0].date(), recent.index[-1].date())
        present = {d.date() for d in recent.index}
        missing = sum(1 for d in expected_days if d.date() not in present)
        if missing:
            issues.append(f"{missing} missing weekday session(s) in the last {len(recent)} bars "
                          "(may be exchange holidays — holiday calendar not modelled)")
    else:
        counts = recent.groupby(recent.index.date).size()
        complete = counts.iloc[:-1] if len(counts) > 1 else counts.iloc[0:0]
        missing = int((BARS_PER_SESSION - complete[complete < BARS_PER_SESSION]).sum())
        if missing:
            issues.append(f"{missing} missing intraday bar(s) across completed sessions")

    freshness = "UNKNOWN"
    if expected_last is not None:
        exp = pd.Timestamp(expected_last)
        if last_ts < exp:
            freshness = "STALE"
            issues.append(f"Latest bar {last_ts.isoformat()} is older than expected {exp.isoformat()}")
        else:
            freshness = "FRESH"
    elif now is not None:
        age_h = (pd.Timestamp(now) - last_ts).total_seconds() / 3600
        limit = 96 if timeframe == "1d" else 0.5
        freshness = "STALE" if age_h > limit else "FRESH"
        if freshness == "STALE":
            issues.append(f"Latest bar is {age_h:.1f}h old")

    if n_bad or dups:
        status = "INVALID"
    elif freshness == "STALE":
        status = "STALE"
    elif len(df) < min_bars:
        status = "INSUFFICIENT"
        issues.append(f"Only {len(df)} bars; at least {min_bars} required")
    elif missing > (2 if timeframe == "1d" else BARS_PER_SESSION):
        status = "GAPS"
    else:
        status = "OK"
    return DataQuality(status == "OK", status, freshness, len(df), last_ts.isoformat(), _iso(expected_last),
                       missing, issues)


def _iso(d: datetime | None) -> str | None:
    return pd.Timestamp(d).isoformat() if d is not None else None
