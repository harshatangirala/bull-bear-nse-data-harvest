"""Importance rules for top gainers / losers.

Being on NSE's gainer list is not by itself interesting -- something is always
top of the list. What matters is the size of the move, on a name liquid
enough for the move to mean something.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal
from .correlate import make_dedup_key


class GainersLosersProcessor(BaseProcessor):
    config_key = "gainers_losers"
    rule_id = "pct_move"

    def __init__(self, cfg, universe=None, category: str = "gainers"):
        self.category = category
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("pct_move_notable", 5.0))
        self.critical = float(self.rules.get("pct_move_critical", 9.0))
        self.min_value_cr = float(self.rules.get("min_traded_value_cr", 0.0))
        self.buckets = set(self.rules.get("buckets") or [])

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        # A symbol can appear in several buckets; alert once, on the widest.
        best: dict[str, dict] = {}

        for row in rows:
            if self.buckets and row.get("bucket") not in self.buckets:
                continue
            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            pct = row.get("pct_change")
            if pct is None:
                continue
            magnitude = abs(pct)
            if magnitude < self.notable:
                continue

            # Illiquid names produce huge percentage moves on tiny volume.
            value_cr = row.get("traded_value")
            if self.min_value_cr > 0:
                if value_cr is None or value_cr < self.min_value_cr:
                    continue

            prev = best.get(symbol)
            if prev is None or magnitude > abs(prev.get("pct_change") or 0):
                best[symbol] = row

        for symbol, row in best.items():
            pct = row["pct_change"]
            magnitude = abs(pct)
            severity = "critical" if magnitude >= self.critical else "notable"
            direction = "gained" if pct > 0 else "fell"
            arrow = "▲" if pct > 0 else "▼"
            ltp = row.get("last_price")
            value_cr = row.get("traded_value")

            body_bits = [f"LTP {ltp:,.2f}" if ltp is not None else "LTP n/a"]
            if value_cr is not None:
                body_bits.append(f"turnover Rs {value_cr:,.0f} cr")
            body_bits.append(f"bucket {row.get('bucket')}")
            ca = (row.get("extra") or {}).get("ca_purpose") or ""
            if ca:
                # A dividend/split ex-date explains a lot of "big" moves.
                body_bits.append(f"CA: {ca}")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                # Sign in the bucket so a reversal opens a new state rather
                # than being swallowed as an escalation of the old one.
                state_bucket=f"move_{'up' if pct > 0 else 'down'}",
                severity=severity,
                title=f"{arrow} {symbol} {direction} {magnitude:.2f}%",
                body=" | ".join(body_bits),
                value=pct,
                # Keyed on the symbol alone: a stock moving hard also shows up
                # in volume spurts and at its price band, and those are the
                # same event seen from different angles.
                dedup_key=make_dedup_key("equity_move", symbol),
                dedup_priority=1,
                payload={
                    "symbol": symbol, "pct_change": pct, "ltp": ltp,
                    "turnover_cr": value_cr, "bucket": row.get("bucket"),
                    "direction": row.get("extra", {}).get("direction"),
                },
            ))
        return signals
