"""Importance rules for bulk / block / short deals.

Note on universe filtering: unlike the intraday rules, this one defaults to
ignoring the alert universe. A Rs 80 cr bulk deal in a non-Nifty-500 smallcap
is exactly the kind of thing worth knowing about, and the value threshold
already does the noise control. Set rules.large_deals.respect_universe: true
to change that.

Deduplication note: a block deal large enough to cross the bulk threshold is
published in *both* NSE feeds -- one trade, two rows. This module no longer
special-cases that. It stamps every signal with a `dedup_key` describing the
underlying trade and a `dedup_priority` (block outranks bulk, because it is
the more specific classification), and the shared CrossFeedDeduplicator in
correlate.py collapses them. The same mechanism serves every other overlap.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal
from .correlate import make_dedup_key

# Higher wins the headline when several feeds report one trade.
_FEED_PRIORITY = {"BLOCK": 2, "BULK": 1, "SHORT": 0}
_FEED_LABEL = {"BLOCK": "block feed", "BULK": "bulk feed",
               "SHORT": "short feed"}


class LargeDealsProcessor(BaseProcessor):
    category = "large_deals"
    config_key = "large_deals"
    rule_id = "deal_value"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("value_cr_notable", 10.0))
        self.critical = float(self.rules.get("value_cr_critical", 50.0))
        self.block_notable = float(
            self.rules.get("block_value_cr_notable", self.notable)
        )
        self.deal_types = {t.upper() for t in
                           (self.rules.get("deal_types") or
                            ["BULK", "BLOCK", "SHORT"])}
        self.watch_clients = [c.strip().upper() for c in
                              (self.rules.get("watch_clients") or []) if c.strip()]
        self.respect_universe = bool(self.rules.get("respect_universe", False))

    def _floor_for(self, deal_type: str) -> float:
        # Block deals are pre-negotiated and structurally larger, so they get
        # their own floor to avoid drowning out bulk-deal signal.
        return self.block_notable if deal_type == "BLOCK" else self.notable

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []

        for row in rows:
            deal_type = (row.get("deal_type") or "").upper()
            if deal_type not in self.deal_types:
                continue

            symbol = row.get("symbol") or ""
            if self.respect_universe and not self.in_universe(symbol):
                continue

            value_cr = row.get("value_cr")
            client = (row.get("client_name") or "").strip()
            client_upper = client.upper()

            watched = any(w in client_upper for w in self.watch_clients)
            if not watched:
                if value_cr is None:
                    continue
                if value_cr < self._floor_for(deal_type):
                    continue

            severity = ("critical" if value_cr is not None
                        and value_cr >= self.critical else "notable")

            side = row.get("buy_sell") or ""
            qty = row.get("quantity")
            price = row.get("price")

            body_bits = [client or "unknown client"]
            if qty is not None and price is not None:
                body_bits.append(f"{qty:,} @ {price:,.2f}")
            if row.get("company"):
                body_bits.append(row["company"])
            if watched:
                body_bits.append("watched client")
            remarks = row.get("remarks") or ""
            if remarks and remarks != "-":
                body_bits.append(remarks)

            value_txt = f"Rs {value_cr:,.1f} cr" if value_cr is not None else "n/a"
            arrow = "▲" if side == "BUY" else "▼" if side == "SELL" else "•"

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                # Deals are discrete events, so the state key is the deal
                # itself -- re-polling after close must not re-alert, but a
                # second deal in the same name must.
                state_bucket=f"{deal_type}:{row.get('dedupe_key', '')[:16]}",
                severity=severity,
                title=f"{arrow} {deal_type} DEAL {symbol} {side} {value_txt}",
                body=" | ".join(body_bits),
                value=value_cr,
                # Identity of the underlying trade, independent of which feed
                # reported it: same symbol, client, side, size and price on the
                # same day is one trade, however many feeds carry it.
                dedup_key=make_dedup_key(
                    "deal", symbol, client_upper, side, qty, price,
                    row.get("trade_date"),
                ),
                dedup_priority=_FEED_PRIORITY.get(deal_type, 0),
                dedup_label=_FEED_LABEL.get(deal_type, deal_type.lower()),
                payload={
                    "symbol": symbol, "deal_type": deal_type,
                    "client_name": client, "buy_sell": side,
                    "quantity": qty, "price": price, "value_cr": value_cr,
                    "trade_date": str(row.get("trade_date") or ""),
                    "company": row.get("company", ""),
                },
            ))
        return signals
