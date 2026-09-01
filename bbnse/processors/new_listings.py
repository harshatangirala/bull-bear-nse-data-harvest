"""Importance rules for new listings.

Every new listing is worth one alert -- there are only a handful a week and
missing one is worse than an occasional dull notification. The universe filter
is deliberately skipped: a stock cannot already be in an index constituent
list on the day it lists.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class NewListingsProcessor(BaseProcessor):
    category = "new_listings"
    config_key = "new_listings"
    rule_id = "new_listing"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.severity = self.rules.get("severity", "notable")

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not symbol:
                continue

            extra = row.get("extra") or {}
            ltp = row.get("last_price")
            issue = extra.get("issue_price")
            pct = row.get("pct_change")

            body_bits = [row.get("company") or ""]
            if ltp is not None:
                body_bits.append(f"LTP {ltp:,.2f}")
            if issue:
                body_bits.append(f"issue {issue:,.2f}")
                if ltp:
                    body_bits.append(f"listing gain {(ltp - issue) / issue * 100:+.1f}%")
            elif pct is not None:
                body_bits.append(f"{pct:+.2f}%")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket="new_listing",
                severity=self.severity,
                title=f"★ NEW LISTING {symbol}",
                body=" | ".join(b for b in body_bits if b),
                value=ltp,
                payload={"symbol": symbol, "company": row.get("company"),
                         "ltp": ltp, "issue_price": issue,
                         "listing_date": extra.get("listing_date")},
            ))
        return signals
