"""Index constituents (NSE / niftyindices.com public CSVs), cached on disk with an as-of date.

These are CURRENT constituents only (not point-in-time): backtests on them carry survivorship bias, which is
disclosed in every report. Sector = NSE "Industry" classification from the same file.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import BACKEND_DIR

log = logging.getLogger(__name__)
CONST_DIR = BACKEND_DIR / "config" / "universe"
URLS = {
    "nifty50": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "nifty100": "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "nifty200": "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    "nifty500": "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
}
REFRESH_SECONDS = 7 * 86400

# NSE industry -> sector index used for sector relative strength / rotation (None = no matching sector index)
INDUSTRY_SECTOR_INDEX: dict[str, str | None] = {
    "Financial Services": "NIFTY FIN SERVICE",
    "Information Technology": "NIFTY IT",
    "Healthcare": "NIFTY PHARMA",
    "Automobile and Auto Components": "NIFTY AUTO",
    "Fast Moving Consumer Goods": "NIFTY FMCG",
    "Metals & Mining": "NIFTY METAL",
    "Oil Gas & Consumable Fuels": "NIFTY ENERGY",
    "Power": "NIFTY ENERGY",
    "Realty": "NIFTY REALTY",
    "Construction": "NIFTY INFRA",
    "Construction Materials": "NIFTY INFRA",
    "Telecommunication": "NIFTY INFRA",
    "Capital Goods": "NIFTY INFRA",
    "Consumer Durables": "NIFTY CONSUMPTION",
    "Consumer Services": "NIFTY CONSUMPTION",
}


def _path(name: str) -> Path:
    return CONST_DIR / f"{name}.csv"


def _meta_path(name: str) -> Path:
    return CONST_DIR / f"{name}.meta.json"


def refresh(name: str, timeout: float = 20.0) -> bool:
    url = URLS[name]
    r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    if "Symbol" not in r.text[:200]:
        raise ValueError("unexpected constituents file format")
    CONST_DIR.mkdir(parents=True, exist_ok=True)
    _path(name).write_text(r.text.replace("\r\n", "\n"), encoding="utf-8")
    _meta_path(name).write_text(json.dumps({"source": url, "fetched_at": int(time.time())}), encoding="utf-8")
    return True


def load(name: str = "nifty200", allow_network: bool = True) -> list[dict[str, str]]:
    """Rows {symbol, name, industry, series}. Refreshes weekly when online; falls back to the cached file."""
    if name not in URLS:
        raise KeyError(f"Unknown index universe '{name}' (choose from {', '.join(URLS)})")
    p = _path(name)
    stale = True
    if _meta_path(name).exists():
        try:
            stale = time.time() - json.loads(_meta_path(name).read_text())["fetched_at"] > REFRESH_SECONDS
        except (ValueError, KeyError):
            pass
    if allow_network and (stale or not p.exists()):
        try:
            refresh(name)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not refresh %s constituents (%s); using cached copy", name, exc)
    if not p.exists():
        return []
    rows = list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8"))))
    return [{"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(), "industry": r["Industry"].strip(),
             "series": r.get("Series", "EQ").strip()} for r in rows if r.get("Symbol")]


def info(name: str = "nifty200") -> dict[str, Any]:
    meta = {}
    if _meta_path(name).exists():
        meta = json.loads(_meta_path(name).read_text())
    return {"universe": name, "count": len(load(name, allow_network=False)), **meta,
            "point_in_time": False,
            "note": "Current constituents only - historical index changes are not modelled (survivorship bias)."}
