"""Closing Auction Session (CAS).

Endpoint: /api/NextApi/apiClient/casApi?functionName=getCASData

CAS is a special call-auction window NSE runs for select/illiquid securities
right around the close; the endpoint is genuinely empty outside that window
(confirmed empty both on a Saturday and mid-session on a live Tuesday --
the shape below comes from the Next.js bundle's own column definitions, not
a populated live sample).

Unit verified 2026-09-01 by reading the frontend's own conversion code rather
than by arithmetic (no live rows were available to cross-check against). The
column config carries `denom: Lakhs|Crores|Billions` for `finalValue`, and
the render path divides the raw API value by `{Lakhs:1e5, Crores:1e7,
Billions:1e9}[selectedUnit]` before display. Dividing a RUPEE figure by 1e5
is what yields lakhs, so the API's raw `finalValue` (and `totalValue`) must
already be in **rupees** -- consistent with every other endpoint on this
newer Next.js/NextApi gateway (most_active_value, etf, pre_open,
derivatives_watch), as opposed to the older /api/live-analysis-* family
which favours lakh. Re-confirm with real numbers once CAS is observed live
with `data` populated -- see docs/endpoints.md.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, rupees_to_cr, to_float, to_int


class ClosingAuctionFetcher(BaseFetcher):
    category = "closing_auction"
    endpoint_name = "closing_auction"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        out: list[dict] = []
        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            out.append({
                "category": self.category,
                "bucket": "cas",
                "symbol": symbol,
                "last_price": to_float(row.get("finalPrice") or row.get("IEP")),
                "change": to_float(row.get("change")),
                "pct_change": to_float(row.get("perChange")),
                "volume": to_int(row.get("finalQuantity")),
                "traded_value": rupees_to_cr(row.get("finalValue")),
                "extra": {
                    "reference_price": to_float(row.get("refrencePrice")),
                    "lower_band": to_float(row.get("lowerBand")),
                    "upper_band": to_float(row.get("upperBand")),
                    "iep": to_float(row.get("IEP")),
                    "best_bid_price": to_float(row.get("bestBidPrice")),
                    "best_ask_price": to_float(row.get("bestAskPrice")),
                    "iiq_at_ep": to_int(row.get("iiqAtEP")),
                },
            })
        return out
