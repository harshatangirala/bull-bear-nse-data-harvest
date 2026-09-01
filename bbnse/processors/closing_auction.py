"""Importance rules for the Closing Auction Session.

CAS already restricts itself to a small, curated set of securities during a
narrow pre-close window, so the bar for "important" is a straight percent
move off the reference price -- similar in spirit to gainers/losers, but
scoped to a session where price discovery happens by auction, not continuous
trading.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class ClosingAuctionProcessor(BaseProcessor):
    category = "closing_auction"
    config_key = "closing_auction"
    rule_id = "cas_move"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("pct_move_notable", 5.0))
        self.critical = float(self.rules.get("pct_move_critical", 10.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not symbol:
                continue

            pct = row.get("pct_change")
            if pct is None or abs(pct) < self.notable:
                continue

            magnitude = abs(pct)
            severity = "critical" if magnitude >= self.critical else "notable"
            arrow = "▲" if pct > 0 else "▼"
            extra = row.get("extra") or {}

            body_bits = []
            if row.get("last_price") is not None:
                body_bits.append(f"final {row['last_price']:,.2f}")
            if extra.get("reference_price") is not None:
                body_bits.append(f"ref {extra['reference_price']:,.2f}")
            if row.get("traded_value") is not None:
                body_bits.append(f"value Rs {row['traded_value']:,.2f} cr")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"cas_{'up' if pct > 0 else 'down'}",
                severity=severity,
                title=f"{arrow} CAS {symbol} {pct:+.2f}% vs reference",
                body=" | ".join(body_bits),
                value=pct,
                payload={"symbol": symbol, "pct_change": pct,
                         "final_price": row.get("last_price"),
                         "reference_price": extra.get("reference_price")},
            ))
        return signals
