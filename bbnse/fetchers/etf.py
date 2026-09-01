"""Exchange traded funds.

Endpoint: /api/etf  (349 rows)

Units verified 2026-08-29 against a live sample:
  trdVal   RUPEES -- qty*ltP/trdVal = 1.01 (SILVERBEES: 26,759,523 x 230.26
           against a reported 6,126,860,386)
  qty      share count (absolute)
  nav      rupees per unit
Almost every numeric field arrives as a STRING here, including prices.

Field names are heavily abbreviated and inconsistent with the rest of the
API: `ltP` (capital P), `chn` for change, `per` for percent, `wkhi`/`wklo`
for the 52-week range.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, rupees_to_cr, to_float, to_int


class EtfFetcher(BaseFetcher):
    category = "etf"
    endpoint_name = "etf"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        out: list[dict] = []
        for row in self.rows_from(payload):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            ltp = to_float(row.get("ltP"))
            nav = to_float(row.get("nav"))
            # The reason to track ETFs at all: price diverging from NAV.
            premium_pct = None
            if ltp is not None and nav:
                premium_pct = (ltp - nav) / nav * 100.0

            out.append({
                "category": self.category,
                "bucket": "etf",
                "symbol": symbol,
                "company": (row.get("assets") or "")[:200],
                "last_price": ltp,
                "prev_close": to_float(row.get("prevClose")),
                "change": to_float(row.get("chn")),
                "pct_change": to_float(row.get("per")),
                "volume": to_int(row.get("qty")),
                "traded_value": rupees_to_cr(row.get("trdVal")),   # RUPEES
                "extra": {
                    "nav": nav,
                    "premium_pct": premium_pct,
                    "open": to_float(row.get("open")),
                    "high": to_float(row.get("high")),
                    "low": to_float(row.get("low")),
                    "week_high": to_float(row.get("wkhi")),
                    "week_low": to_float(row.get("wklo")),
                },
            })
        return out
