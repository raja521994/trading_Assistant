"""Provider-independent market-data interface.

Core engines (quant, risk, signals, backtest) never import provider-specific code; they only see
these types. Each provider declares its capabilities explicitly so the UI can disclose coverage.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import pandas as pd


class DataStatus(str, Enum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    EOD = "EOD"
    SIMULATED = "SIMULATED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    exchange: str
    name: str
    instrument_type: str  # EQ | INDEX
    sector: str | None = None
    lot_size: int = 1
    tick_size: float = 0.05
    provider_token: str | None = None


@dataclass
class Quote:
    symbol: str
    exchange: str
    last_price: float
    prev_close: float
    ts: datetime
    status: DataStatus
    market_open: bool

    @property
    def change_pct(self) -> float:
        return (self.last_price / self.prev_close - 1.0) * 100.0 if self.prev_close else 0.0


@dataclass
class ProviderCapabilities:
    timeframes: list[str]
    data_status: DataStatus
    exchanges: list[str]
    instrument_types: list[str]
    supports_quotes: bool
    supports_streaming: bool
    supports_depth: bool
    supports_oi: bool
    supports_option_chain: bool
    supports_fundamentals: bool
    supports_corporate_actions: bool
    supports_news: bool
    supports_orders: bool  # always False in this build
    history_note: str
    missing_fields: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    needs_verification: list[str] = field(default_factory=list)


@dataclass
class ProviderHealth:
    name: str
    connected: bool
    status: DataStatus
    message: str
    last_update: datetime | None = None
    latency_ms: float | None = None
    errors: list[str] = field(default_factory=list)


class MarketDataProvider(ABC):
    name: str = "base"
    display_name: str = "Base"

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities: ...

    @abstractmethod
    def health(self) -> ProviderHealth: ...

    @abstractmethod
    def instruments(self) -> list[InstrumentInfo]: ...

    @abstractmethod
    def candles(self, symbol: str, exchange: str, timeframe: str, start: datetime | None = None,
                end: datetime | None = None) -> pd.DataFrame:
        """Return OHLCV with a tz-aware (IST) DatetimeIndex named 'ts' and columns open/high/low/close/volume.
        Only completed bars are returned."""

    @abstractmethod
    def quote(self, symbol: str, exchange: str) -> Quote | None: ...

    @abstractmethod
    def expected_last_bar(self, timeframe: str, now: datetime | None = None) -> datetime | None:
        """Timestamp of the most recent bar that *should* exist now (used for staleness checks)."""

    @property
    def enabled(self) -> bool:
        return True
