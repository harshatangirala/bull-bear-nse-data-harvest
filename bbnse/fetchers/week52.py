"""52-week high / low.

Endpoints:
  /api/live-analysis-data-52weekhighstock
  /api/live-analysis-data-52weeklowstock

Field notes from the live payload:
  comapnyName   NSE's own misspelling of companyName. Handled, not corrected.
  new52WHL      the new 52-week extreme just set
  prev52WHL     the previous extreme it replaced
  prevHLDate    when the previous extreme was set (e.g. "10-Jun-2026")
  prevClose     arrives as a *string*
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float


class _Week52Fetcher(BaseFetcher):
    kind = ""          # "high" | "low"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        rows = self.rows_from(payload)
        out: list[dict] = []
        for row in rows:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            new_extreme = to_float(row.get("new52WHL"))
            prev_extreme = to_float(row.get("prev52WHL"))

            # How far the new extreme cleared the old one. Signed so that a
            # positive margin always means "more extreme in the expected
            # direction", for highs and lows alike.
            margin_pct = None
            if new_extreme and prev_extreme and prev_extreme > 0:
                raw = (new_extreme - prev_extreme) / prev_extreme * 100.0
                margin_pct = raw if self.kind == "high" else -raw

            out.append({
                "category": self.category,
                "bucket": self.kind,
                "symbol": symbol,
                "company": row.get("comapnyName") or row.get("companyName") or "",
                "last_price": to_float(row.get("ltp")),
                "prev_close": to_float(row.get("prevClose")),
                "change": to_float(row.get("change")),
                "pct_change": to_float(row.get("pChange")),
                "extreme_value": new_extreme,
                "prev_extreme": prev_extreme,
                "prev_extreme_date": str(row.get("prevHLDate") or ""),
                "extra": {
                    "series": row.get("series"),
                    "kind": self.kind,
                    "margin_pct": margin_pct,
                },
            })
        return out


class Week52HighFetcher(_Week52Fetcher):
    category = "week52_high"
    endpoint_name = "week52_high"
    kind = "high"


class Week52LowFetcher(_Week52Fetcher):
    category = "week52_low"
    endpoint_name = "week52_low"
    kind = "low"
