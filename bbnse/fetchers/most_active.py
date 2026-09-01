"""Most active equities by traded value.

Endpoint: /api/live-analysis-most-active-securities?index=value

Units verified 2026-08-29 against a live sample:
  totalTradedValue   RUPEES  -- median(quantityTraded*lastPrice/value) = 1.01
                     across 20 rows.

This is the trap the unit audit exists for. Three equity feeds each report
"the value traded" and all three disagree: gainers uses LAKH, this one uses
RUPEES, price_band uses CRORE. Reusing lakh_to_cr here would have understated
every number by 100,000x and the category would silently never alert.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, rupees_to_cr, to_float, to_int


class MostActiveValueFetcher(BaseFetcher):
    category = "most_active_value"
    endpoint_name = "most_active_value"

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
            out.append({
                "category": self.category,
                "bucket": "value",
                "symbol": symbol,
                "last_price": to_float(row.get("lastPrice")),
                "prev_close": to_float(row.get("previousClose")),
                "change": to_float(row.get("change")),
                "pct_change": to_float(row.get("pChange")),
                "volume": to_int(row.get("totalTradedVolume")
                                 or row.get("quantityTraded")),
                # RUPEES -> crore. Verified; do not swap for lakh_to_cr.
                "traded_value": rupees_to_cr(row.get("totalTradedValue")),
                "extra": {
                    "open": to_float(row.get("open")),
                    "close": to_float(row.get("closePrice")),
                    "year_high": to_float(row.get("yearHigh")),
                    "year_low": to_float(row.get("yearLow")),
                    "ex_date": row.get("exDate") or "",
                    "purpose": row.get("purpose") or "",
                },
            })
        return out
