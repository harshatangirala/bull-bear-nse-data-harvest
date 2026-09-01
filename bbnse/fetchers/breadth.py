"""Market breadth (advances / declines).

Endpoint: /api/live-analysis-advance

The payload is 525 KB but almost all of it is the list of ~1900 advancing
stocks, which duplicates what gainers already gives us. The breadth numbers
live in a small `advance.count` block, so this fetcher emits a single summary
row rather than ~1900 near-duplicate observations per poll. That keeps the
observation table meaningful and is why this category sits on the slow tier.

Units verified 2026-08-29 against a live sample:
  count.{Advances,Declines,Unchange,Total}  plain integers
  row.totalTradedVolume                     LAKH shares
  row.totalTradedValue                      CRORE
Cross-check: TEJASNET 431.647 (lakh) shares against 2418 (crore) implies an
average price of ~560 against an LTP of 549.

Note `pchange` is lower-case here, unlike `pChange` everywhere else.
"""
from __future__ import annotations

from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float

BREADTH_ENTITY = "MARKET"


class AdvanceDeclineFetcher(BaseFetcher):
    category = "advance_decline"
    endpoint_name = "advance_decline"

    def payload_trade_date(self, payload: Any):
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        node = payload.get("advance")
        if not isinstance(node, dict):
            return []
        counts = node.get("count") or {}

        advances = int(to_float(counts.get("Advances")) or 0)
        declines = int(to_float(counts.get("Declines")) or 0)
        unchanged = int(to_float(counts.get("Unchange")) or 0)
        total = int(to_float(counts.get("Total")) or
                    (advances + declines + unchanged))

        # Guard against a zero denominator on a feed glitch.
        ratio = (advances / declines) if declines else float(advances or 0)

        return [{
            "category": self.category,
            "bucket": "breadth",
            "symbol": BREADTH_ENTITY,
            "company": "NSE market breadth",
            "extra": {
                "advances": advances,
                "declines": declines,
                "unchanged": unchanged,
                "total": total,
                "ad_ratio": ratio,
                "advance_pct": (advances / total * 100) if total else None,
            },
        }]
