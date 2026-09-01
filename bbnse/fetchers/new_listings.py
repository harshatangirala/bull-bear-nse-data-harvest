"""New stock exchange listings.

Endpoint: /api/new-listing-today

This endpoint returns the JSON literal `null` on a non-trading day, not an
empty object and not an empty list. Verified 2026-08-29: the response body was
13 bytes containing `null`. Anything walking the payload must tolerate None,
which is why normalize() checks the type before touching it.

No money fields, so no unit ambiguity. Listing prices are plain rupees.
"""
from __future__ import annotations

from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float, to_int


class NewListingsFetcher(BaseFetcher):
    category = "new_listings"
    endpoint_name = "new_listings"

    def normalize(self, payload: Any) -> list[dict]:
        # `null` on quiet days, a dict with `data` when there are listings.
        if payload is None:
            return []
        rows = (payload if isinstance(payload, list)
                else self.rows_from(payload))

        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            out.append({
                "category": self.category,
                "bucket": "listing",
                "symbol": symbol,
                "company": row.get("companyName") or row.get("name") or "",
                "last_price": to_float(row.get("lastPrice")
                                       or row.get("ltp")),
                "change": to_float(row.get("change")),
                "pct_change": to_float(row.get("pChange")),
                "volume": to_int(row.get("totalTradedVolume")),
                "extra": {
                    "series": row.get("series"),
                    "issue_price": to_float(row.get("issuePrice")),
                    "listing_date": str(
                        parse_nse_date(row.get("listingDate")) or ""),
                },
            })
        return out
