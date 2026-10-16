"""Signal logic: state machine, latest-bar evaluation, strategy rules, guardrails."""

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from app.ai.provider import unsupported_numbers
from app.data.sample_generator import generate_daily
from app.data.validation import validate_candles
from app.logging_setup import RedactingFilter
from app.quant.features import arrays
from app.quant.scanner import evaluate_latest
from app.quant.strategies.base import Setup, Strategy, all_strategy_ids, get_strategy
from app.signals.state_machine import (
    ACTIVE_STATE, ALLOWED, EXITED, EXPIRED, INVALIDATED, SETUP, TRIGGERED, WATCH, can_transition)

SPEC = {"id": "t", "name": "T", "version": "0", "category": "t", "modes": ["swing"], "timeframe": "1d",
        "direction": "long_only", "params": {}}


class At(Strategy):
    def __init__(self, at):
        super().__init__(SPEC)
        self.at = at

    @property
    def warmup(self):
        return 1

    def entry(self, a, i):
        return Setup("LONG", a["close"][i] - 2.0, 2.0, ["x"], "x", 10) if i == self.at else None


def flat(n=30):
    idx = pd.bdate_range("2024-01-01", periods=n).tz_localize("Asia/Kolkata")
    return pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1e5}, index=idx)


def test_state_machine_transitions():
    assert can_transition(WATCH, SETUP) and can_transition(SETUP, TRIGGERED)
    assert can_transition(TRIGGERED, ACTIVE_STATE) and can_transition(ACTIVE_STATE, EXITED)
    assert can_transition(SETUP, INVALIDATED) and can_transition(SETUP, EXPIRED)
    assert can_transition(TRIGGERED, INVALIDATED)  # entry open gapped through the stop
    assert not can_transition(TRIGGERED, SETUP) and not can_transition(TRIGGERED, EXITED)
    assert not can_transition(EXITED, TRIGGERED)
    for terminal in (INVALIDATED, EXITED, EXPIRED):
        assert ALLOWED[terminal] == set()


def test_evaluate_latest_setup_triggered_active():
    df = flat()
    c = evaluate_latest(At(29), arrays(df))
    assert c["state"] == "SETUP" and c["entry_ref"] == 100.5  # trigger = signal-bar high
    df.iloc[29, df.columns.get_loc("close")] = 100.6
    c2 = evaluate_latest(At(28), arrays(df))
    assert c2["state"] == "TRIGGERED" and c2["trigger_i"] == 29 and c2["entry_i"] is None
    df3 = flat()
    df3.iloc[28, df3.columns.get_loc("close")] = 100.6
    df3.iloc[29, df3.columns.get_loc("open")] = 100.7
    c3 = evaluate_latest(At(27), arrays(df3))
    assert c3["state"] == "ACTIVE" and c3["entry_i"] == 29 and c3["entry_ref"] == pytest.approx(100.7)


def test_evaluate_latest_drops_resolved_setups():
    df = flat()
    df.iloc[28, df.columns.get_loc("close")] = 100.6
    df.iloc[29, df.columns.get_loc("low")] = 97.0  # stop (98) hit after entry
    assert evaluate_latest(At(27), arrays(df)) is None
    df2 = flat()
    df2.iloc[28, df2.columns.get_loc("close")] = 100.6
    df2.iloc[29, df2.columns.get_loc("open")] = 97.5  # entry open below stop -> invalidated
    assert evaluate_latest(At(27), arrays(df2)) is None
    df3 = flat()
    df3.iloc[29, df3.columns.get_loc("low")] = 97.5  # stop before trigger -> invalidated
    assert evaluate_latest(At(28), arrays(df3)) is None
    assert evaluate_latest(At(25), arrays(flat())) is None  # 3 bars without trigger -> expired
    assert evaluate_latest(At(10), arrays(flat())) is None  # older than the window


@pytest.fixture(scope="module")
def sample():
    d = generate_daily(42, date(2023, 1, 2), date(2025, 6, 30))
    return {k: v for k, v in d.items() if v["volume"].sum() > 0}


def test_all_strategies_emit_valid_setups(sample):
    for sid in all_strategy_ids():
        strat = get_strategy(sid)
        if strat.intraday:
            continue
        found = 0
        for df in list(sample.values())[:15]:
            p = strat.prepare(df)
            a = arrays(p)
            for i in range(strat.warmup + 1, len(df)):
                s = strat.entry(a, i)
                if s is None:
                    continue
                found += 1
                assert s.direction == "LONG"
                assert s.stop < a["close"][i]
                assert s.reasons and s.invalidation
        assert found > 0, f"{sid} produced no setups on sample data"


def test_strategy_has_no_lookahead(sample):
    """Entry decisions at bar i must not change when future bars are altered."""
    df = next(iter(sample.values()))
    cut = 400
    for sid in ("ema_cross", "donchian_breakout", "rsi2_meanrev", "supertrend_trend"):
        strat = get_strategy(sid)
        full = arrays(strat.prepare(df))
        trunc = arrays(strat.prepare(df.iloc[:cut]))
        for i in range(strat.warmup + 1, cut):
            a1, a2 = strat.entry(full, i), strat.entry(trunc, i)
            assert (a1 is None) == (a2 is None), f"{sid} look-ahead at bar {i}"


def test_orb_requires_completed_opening_range():
    from app.providers.sample import SampleProvider

    sp = SampleProvider(intraday_sessions=3, clock=lambda: pd.Timestamp("2025-06-27 18:00", tz="Asia/Kolkata").to_pydatetime())
    df = sp.candles("RELIANCE", "NSE", "5m")
    strat = get_strategy("orb_vwap")
    p = strat.prepare(df)
    first_session = p[p.index.date == p.index[0].date()]
    assert first_session["or_high"].iloc[:3].isna().all()
    assert first_session["or_high"].iloc[3] == pytest.approx(first_session["high"].iloc[:3].max())


def test_validation_flags_stale_invalid_and_gaps():
    df = flat(100)
    ok = validate_candles(df, "1d", df.index[-1].to_pydatetime())
    assert ok.ok and ok.status == "OK"
    stale = validate_candles(df, "1d", (df.index[-1] + pd.Timedelta(days=3)).to_pydatetime())
    assert not stale.ok and stale.status == "STALE"
    bad = df.copy()
    bad.iloc[-1, bad.columns.get_loc("high")] = 50.0
    assert validate_candles(bad, "1d", bad.index[-1].to_pydatetime()).status == "INVALID"
    gaps = df.drop(df.index[[50, 51, 52, 60]])
    assert validate_candles(gaps, "1d", gaps.index[-1].to_pydatetime()).status == "GAPS"
    assert validate_candles(df.iloc[:30], "1d", df.index[29].to_pydatetime()).status == "INSUFFICIENT"


def test_llm_number_guardrail():
    facts = "Close 2237.70 vs EMA50 2006.45; risk Rs 6,720"
    assert unsupported_numbers("Price 2237.70 is above the EMA50 2006.45", facts) == []
    assert unsupported_numbers("Expect a move to 2500 soon", facts) == [2500.0]
    assert unsupported_numbers("Risk is about 6,720 rupees", facts) == []


def test_log_redaction():
    f = RedactingFilter(["supersecretvalue"])
    assert "supersecretvalue" not in f.redact("token supersecretvalue used")
    assert "abc123" not in f.redact("access_token=abc123&x=1")
    assert "eyJhbGc" not in f.redact("Authorization: Bearer eyJhbGc.x.y")


def test_sample_data_is_deterministic():
    a = generate_daily(42, date(2023, 1, 2), date(2023, 6, 30))
    b = generate_daily(42, date(2023, 1, 2), date(2023, 6, 30))
    for k in a:
        assert np.array_equal(a[k].to_numpy(), b[k].to_numpy())


def test_sample_quote_matches_intraday_path():
    from app.providers.sample import SampleProvider

    now = pd.Timestamp("2025-06-27 11:02:30", tz="Asia/Kolkata").to_pydatetime()
    sp = SampleProvider(intraday_sessions=2, clock=lambda: now)
    bars = sp.candles("TCS", "NSE", "5m")
    last = bars.index[-1]
    assert last == pd.Timestamp("2025-06-27 10:55", tz="Asia/Kolkata")  # last COMPLETED bar
    q = sp.quote("TCS", "NSE")
    assert q.market_open and q.status.value == "SIMULATED"
    daily = sp.candles("TCS", "NSE", "1d")
    assert daily.index[-1].date() == date(2025, 6, 26)  # today's daily bar not revealed until close
    assert q.prev_close == pytest.approx(daily["close"].iloc[-1])
    assert sp.expected_last_bar("5m", now) == last.to_pydatetime()
    weekend = datetime(2025, 6, 28, 12, 0, tzinfo=now.tzinfo)
    assert sp.expected_last_bar("1d", weekend).date() == date(2025, 6, 27)
