"""Market indices.

Endpoint: /api/allIndices  (139 indices)

Units verified 2026-08-29 against a live sample:
  last, open, high, low, previousClose   INDEX POINTS (not rupees)
  variation                              INDEX POINTS
  percentChange                          PERCENT
  pe, pb, dy                             ratios, delivered as STRINGS
There is no turnover field here, so no money-unit ambiguity to resolve.

`indicativeClose` is 0 outside the closing-auction window; it is carried
through but never treated as a price.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float


class IndicesFetcher(BaseFetcher):
    category = "indices_all"
    endpoint_name = "indices_all"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        out: list[dict] = []
        for row in self.rows_from(payload):
            name = (row.get("index") or "").strip()
            if not name:
                continue
            out.append({
                "category": self.category,
                "bucket": row.get("key") or "",
                # Index names contain spaces ("NIFTY 50"); they are the entity.
                "symbol": (row.get("indexSymbol") or name).strip().upper(),
                "company": name,
                "last_price": to_float(row.get("last")),
                "prev_close": to_float(row.get("previousClose")),
                "change": to_float(row.get("variation")),
                "pct_change": to_float(row.get("percentChange")),
                "extra": {
                    "index_name": name,
                    "open": to_float(row.get("open")),
                    "high": to_float(row.get("high")),
                    "low": to_float(row.get("low")),
                    "year_high": to_float(row.get("yearHigh")),
                    "year_low": to_float(row.get("yearLow")),
                    "pe": to_float(row.get("pe")),
                    "pb": to_float(row.get("pb")),
                    "dy": to_float(row.get("dy")),
                },
            })
        return out
