"""Catalogue of all 52 strategies in the brief: testable modules, portfolio strategies, context inputs and the
strategies that are NOT TESTABLE because the required data is not available in this build.

Parameter grids are small, fixed and taken from the published/classic versions of each rule. They are set BEFORE
any test and are never tuned until something passes (see docs/STRATEGY_TOURNAMENT.md).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from ..strategies.base import Strategy

FAMILIES = {
    "A": "Trend following", "B": "Momentum & relative strength", "C": "Mean reversion", "D": "Intraday",
    "E": "Volume / participation", "F": "Price action", "G": "Fundamental / positional investing",
    "H": "Derivatives-based", "I": "Market-wide / macro context",
}


@dataclass
class Entry:
    no: int
    id: str
    name: str
    family: str  # letter A-I
    kind: str  # event | portfolio | pairs | context | not_testable
    timeframe: str = "1d"
    modes: list[str] = field(default_factory=lambda: ["swing"])
    direction: str = "long_only"
    regime_hypothesis: str = "any"
    data_required: list[str] = field(default_factory=lambda: ["Daily OHLCV"])
    missing_data: list[str] = field(default_factory=list)
    grid: list[dict[str, Any]] | None = None
    stability_params: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def testable(self) -> bool:
        return self.kind in ("event", "portfolio", "pairs")

    @property
    def family_name(self) -> str:
        return FAMILIES[self.family]

    def as_dict(self) -> dict[str, Any]:
        return {"no": self.no, "id": self.id, "name": self.name, "family": self.family,
                "family_name": self.family_name, "kind": self.kind, "timeframe": self.timeframe, "modes": self.modes,
                "direction": self.direction, "regime_hypothesis": self.regime_hypothesis,
                "data_required": self.data_required, "missing_data": self.missing_data, "testable": self.testable,
                "grid": self.combos() if self.testable else [], "stability_params": self.stability_params,
                "note": self.note}

    def combos(self) -> list[dict[str, Any]]:
        if self.grid is not None:
            return self.grid or [{}]
        from ..strategies.base import load_specs

        g = (load_specs().get(self.id) or {}).get("param_grid") or {}
        if not g:
            return [{}]
        keys = list(g)
        return [dict(zip(keys, v, strict=True)) for v in itertools.product(*[g[k] for k in keys])]


I5 = ["5-minute OHLCV (one year, NIFTY 50 names)"]
FUND = "Fundamentals history (ROCE, ROE, EPS, sales/profit, debt, promoter holding/pledge, valuation)"

CATALOG: list[Entry] = [
    # A. trend following
    Entry(1, "ema_cross", "EMA crossover (20/50 and 50/200) with ADX filter", "A", "event", regime_hypothesis="bull",
          grid=[{"fast": 20, "slow": 50}, {"fast": 50, "slow": 200, "max_hold_bars": 120}],
          stability_params=["adx_min", "atr_stop_mult"]),
    Entry(2, "supertrend_trend", "Supertrend flip with EMA trend filter", "A", "event", modes=["swing", "positional"],
          regime_hypothesis="bull", stability_params=["atr_n", "mult"]),
    Entry(3, "donchian_breakout", "Donchian breakout (20/55-day, Turtle) with ATR trailing stop", "A", "event",
          modes=["positional", "swing"], regime_hypothesis="bull",
          grid=[{"entry_n": 20, "exit_n": 10}, {"entry_n": 55, "exit_n": 20}],
          stability_params=["entry_n", "atr_stop_mult"]),
    Entry(4, "hi52_breakout", "52-week-high breakout with volume confirmation", "A", "event",
          modes=["swing", "positional"], regime_hypothesis="bull", stability_params=["lookback", "vol_mult"],
          note="All-time-high variant not separately tested: history starts in 2018, so 'ATH' would be mislabelled."),
    Entry(5, "minervini_vcp", "Minervini trend template / Stage-2 + VCP breakout", "A", "event",
          modes=["swing", "positional"], regime_hypothesis="bull", stability_params=["max_base_pct", "vol_mult"]),
    Entry(6, "darvas_box", "Darvas box breakout", "A", "event", regime_hypothesis="bull",
          stability_params=["box_bars", "vol_mult"]),
    Entry(7, "ichimoku_breakout", "Ichimoku cloud breakout", "A", "event", regime_hypothesis="bull",
          stability_params=["kijun", "senkou"]),
    Entry(8, "weekly_hh_breakout", "Weekly/monthly higher-high breakout", "A", "event", modes=["swing", "positional"],
          regime_hypothesis="bull", grid=[{"period": "W"}, {"period": "M", "max_hold_bars": 160}],
          stability_params=["max_hold_bars"]),
    Entry(9, "ribbon_macd", "MA ribbon + MACD trend continuation", "A", "event", regime_hypothesis="bull",
          stability_params=["atr_stop_mult", "max_hold_bars"]),
    Entry(10, "ema_pullback", "Pullback to 20/50 EMA in a strong uptrend", "A", "event", regime_hypothesis="bull",
          grid=[{"ema": 20}, {"ema": 50}], stability_params=["adx_min", "target_r"]),
    # B. momentum & relative strength
    Entry(11, "xs_momentum", "Cross-sectional momentum (6-12 month, skip latest month, top decile)", "B", "portfolio",
          modes=["positional"], regime_hypothesis="bull", grid=[{"lookback": 252}, {"lookback": 126}],
          stability_params=["top_n", "skip"]),
    Entry(12, "rs_new_high", "Relative strength vs NIFTY and sector (RS line at new highs)", "B", "event",
          modes=["swing", "positional"], regime_hypothesis="bull",
          data_required=["Daily OHLCV", "NIFTY 50 daily", "Sector index daily"],
          stability_params=["rs_lookback", "atr_stop_mult"]),
    Entry(13, "momentum_basket", "Momentum factor basket (Nifty200 Momentum 30 style, monthly)", "B", "portfolio",
          modes=["positional"], regime_hypothesis="bull", stability_params=["top_n"],
          note="Rebalanced monthly (the official index rebalances semi-annually)."),
    Entry(14, "lowvol_momentum", "Low-volatility + momentum combination", "B", "portfolio", modes=["positional"],
          regime_hypothesis="any", stability_params=["top_n"]),
    Entry(15, "sector_rotation", "Sector rotation (trade the leaders of the top sectors)", "B", "portfolio",
          modes=["positional"], regime_hypothesis="bull", data_required=["Daily OHLCV", "Sector index daily"],
          stability_params=["lookback", "per_sector"]),
    Entry(16, "dual_momentum", "Dual momentum (absolute + relative) with liquid-fund fallback", "B", "portfolio",
          modes=["positional"], regime_hypothesis="any", data_required=["Daily OHLCV", "NIFTY 50 daily"],
          stability_params=["top_n", "cash_yield"]),
    # C. mean reversion
    Entry(17, "rsi2_meanrev", "RSI(2) pullback in uptrend (price > 200DMA)", "C", "event", regime_hypothesis="bull",
          stability_params=["entry_rsi", "atr_stop_mult"]),
    Entry(18, "bollinger_reversion", "Bollinger reversion with trend filter", "C", "event", regime_hypothesis="sideways",
          stability_params=["max_hold_bars"]),
    Entry(19, "vwap_reversion", "VWAP reversion (intraday)", "C", "event", "5m", ["intraday"], "long_short",
          "sideways", I5, stability_params=["z", "adx_max"]),
    Entry(20, "gap_fill", "Gap-fill (opening gap vs previous close)", "C", "event", "5m", ["intraday"], "long_short",
          "any", I5, stability_params=["min_gap_pct", "max_gap_pct"]),
    Entry(21, "oversold_bounce", "Oversold bounce / 52-week-low reversal with capitulation volume", "C", "event",
          regime_hypothesis="bear", stability_params=["near_low_pct", "vol_mult"]),
    Entry(22, "pairs_reversion", "Pairs / sector-spread reversion (stock futures, paper only)", "C", "pairs",
          direction="long_short", regime_hypothesis="any", data_required=["Daily OHLCV (both legs)"],
          stability_params=["entry_z", "window"],
          note="Short legs require F&O (futures); modelled with futures charges, margin not modelled."),
    # D. intraday
    Entry(23, "orb_vwap", "Opening-range breakout (15m/30m) with VWAP and volume", "D", "event", "5m", ["intraday"],
          "long_short", "any", I5, stability_params=["vol_mult", "target_r"]),
    Entry(24, "vwap_pullback", "VWAP trend-pullback", "D", "event", "5m", ["intraday"], "long_short", "any", I5,
          stability_params=["trend_bars", "target_r"]),
    Entry(25, "cpr_trend", "CPR + pivots (narrow CPR = trend day)", "D", "event", "5m", ["intraday"], "long_short",
          "any", I5, stability_params=["narrow_pct", "target_r"]),
    Entry(26, "pdh_pdl_breakout", "Previous-day high/low breakout", "D", "event", "5m", ["intraday"], "long_short",
          "any", I5, stability_params=["vol_mult", "stop_frac"]),
    Entry(27, "nr7_breakout", "Inside-bar / NR4 / NR7 breakout", "D", "event", "5m", ["intraday"], "long_short", "any",
          I5, grid=[{"pattern": "nr7"}, {"pattern": "nr4"}, {"pattern": "inside"}], stability_params=["target_r"]),
    Entry(28, "gap_and_go_news", "Gap-and-go on news stocks with high relative volume", "D", "not_testable", "5m",
          ["intraday"], missing_data=["News / announcements feed with timestamps (not connected)"]),
    Entry(29, "supertrend_intraday", "Supertrend(10,3) intraday with VWAP filter", "D", "event", "5m", ["intraday"],
          "long_short", "any", I5, stability_params=["st_n", "st_mult"]),
    Entry(30, "hour_momentum", "First-hour momentum / last-hour trend continuation", "D", "event", "5m", ["intraday"],
          "long_short", "any", I5, grid=[{"window": "first"}, {"window": "last"}], stability_params=["min_move_pct"]),
    # E. volume / participation
    Entry(31, "relvol_breakout", "Relative-volume breakout (>2x average, close near high)", "E", "event",
          regime_hypothesis="bull", stability_params=["vol_mult", "lookback"]),
    Entry(32, "delivery_spike", "Delivery-% spike with price strength", "E", "not_testable",
          missing_data=["NSE bhavcopy delivery % history (adapter not wired)"]),
    Entry(33, "obv_divergence", "OBV / accumulation-distribution divergence", "E", "event", regime_hypothesis="any",
          stability_params=["lookback", "atr_stop_mult"]),
    Entry(34, "block_deals", "Bulk/block deal and promoter-buying events", "E", "not_testable",
          missing_data=["NSE/BSE bulk & block deals and insider (SAST/PIT) disclosures history"]),
    # F. price action
    Entry(35, "pivot_sr", "Swing-pivot support/resistance bounce or break (confirmed pivots)", "F", "event",
          regime_hypothesis="any", grid=[{"mode": "break"}, {"mode": "bounce"}],
          stability_params=["pivot_k", "vol_mult"]),
    Entry(36, "candle_sr", "Candlestick patterns at S/R with volume", "F", "event", regime_hypothesis="any",
          stability_params=["near_pct", "vol_mult"]),
    Entry(37, "structure_bos", "HH/HL structure and break of structure", "F", "event", regime_hypothesis="bull",
          stability_params=["pivot_k", "max_hold_bars"]),
    # G. fundamental (no fundamentals data in this build)
    Entry(38, "quality_growth", "Quality + growth screen", "G", "not_testable", modes=["long-term"], missing_data=[FUND]),
    Entry(39, "canslim", "CANSLIM-style (EPS acceleration + price strength)", "G", "not_testable", modes=["long-term"],
          missing_data=["Quarterly EPS history with point-in-time result dates"]),
    Entry(40, "value_screen", "Value screen (P/E, P/B, EV/EBITDA vs sector and own history)", "G", "not_testable",
          modes=["long-term"], missing_data=["Point-in-time valuation multiples / financial statements"]),
    Entry(41, "earnings_drift", "Post-earnings announcement drift", "G", "not_testable", modes=["swing"],
          missing_data=["Results dates and earnings surprise (actual vs consensus)"]),
    Entry(42, "dividend_yield", "Dividend yield + stability screen", "G", "not_testable", modes=["long-term"],
          missing_data=["Dividend history / corporate actions"]),
    Entry(43, "qarp_momentum", "QARP + momentum composite", "G", "not_testable", modes=["long-term"], missing_data=[FUND]),
    Entry(44, "magic_formula", "Magic-Formula-style (earnings yield + ROCE rank)", "G", "not_testable",
          modes=["long-term"], missing_data=[FUND]),
    # H. derivatives
    Entry(45, "oi_buildup", "OI buildup classification", "H", "not_testable", modes=["intraday", "swing"],
          missing_data=["Futures open-interest history (Kite continuous futures + oi=1 not ingested in this build)"]),
    Entry(46, "pcr_maxpain", "PCR / max pain / India VIX regime", "H", "not_testable",
          missing_data=["Option-chain history (expired options are not available from Kite historical data)"],
          note="India VIX itself is used as a regime input (group I)."),
    Entry(47, "futures_basis", "Futures basis / rollover", "H", "not_testable",
          missing_data=["Near/next-month futures price history (not ingested)"]),
    Entry(48, "cash_futures_arb", "Cash-futures arbitrage (carry), net of costs", "H", "not_testable",
          missing_data=["Futures price history and margin/financing assumptions (not ingested)"]),
    Entry(49, "option_structures", "Defined-risk option structures (spreads, condors, straddles)", "H", "not_testable",
          modes=["intraday", "swing"], missing_data=["Option-chain / premium history with IV (not available)"]),
    # I. context (regime inputs, excluded from ranking)
    Entry(50, "ctx_trend_breadth_vix", "NIFTY vs 50/100/200 DMA, breadth, India VIX", "I", "context",
          data_required=["NIFTY 50 daily", "Universe daily closes", "INDIA VIX daily"],
          note="Implemented: feeds the regime classifier and the exposure budget."),
    Entry(51, "ctx_flows_macro", "FII/DII flows, USD-INR, crude, US 10-year, GIFT Nifty", "I", "context",
          missing_data=["FII/DII flows", "USD-INR", "Brent crude", "US 10-year yield", "GIFT Nifty"],
          note="Not connected; the regime card shows these inputs as missing."),
    Entry(52, "ctx_event_calendar", "Event calendar (results, RBI, Budget, F&O ban, circuits, ex-dates)", "I",
          "context", missing_data=["Results/board-meeting dates", "F&O ban list", "Circuit/surveillance lists",
                                   "Corporate-action dates", "RBI policy / Budget dates"],
          note="Not connected; the event gate reports UNKNOWN rather than passing silently."),
    # Additional widely published methods (added after the original 52; same fixed-grid, no-tuning rules)
    Entry(53, "ttm_squeeze", "TTM Squeeze breakout (John Carter)", "A", "event", regime_hypothesis="bull",
          stability_params=["min_squeeze", "kc_k"]),
    Entry(54, "connors_rsi", "ConnorsRSI pullback in uptrend (Larry Connors)", "C", "event", regime_hypothesis="bull",
          grid=[{"entry_crsi": 10}, {"entry_crsi": 15}], stability_params=["atr_stop_mult", "exit_sma"]),
    Entry(55, "elder_triple_screen", "Elder Triple Screen (Alexander Elder)", "A", "event", regime_hypothesis="bull",
          stability_params=["trend_ema", "atr_stop_mult"]),
]

BY_ID = {e.id: e for e in CATALOG}


def _classes() -> dict[str, type]:
    from .daily import DAILY
    from .intraday import INTRADAY
    from .portfolio import PORTFOLIO

    return {c.SPEC["id"]: c for c in DAILY + INTRADAY + PORTFOLIO}


def library_strategy(strategy_id: str, params: dict[str, Any] | None = None) -> Strategy:
    cls = _classes().get(strategy_id)
    if cls is None:
        raise KeyError(strategy_id)
    return cls(params)


def library_ids() -> list[str]:
    return list(_classes())


def testable() -> list[Entry]:
    return [e for e in CATALOG if e.testable]
