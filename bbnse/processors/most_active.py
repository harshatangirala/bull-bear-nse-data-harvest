"""Importance rules for most-active-by-value.

Being on a top-20-by-turnover list is not itself news -- twenty names are
always on it. What is worth knowing is when the absolute rupee turnover is
unusually large, so this rule is a straight value threshold.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal
from .correlate import make_dedup_key


class MostActiveProcessor(BaseProcessor):
    category = "most_active_value"
    config_key = "most_active"
    rule_id = "turnover"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.min_value_cr = float(self.rules.get("min_traded_value_cr", 100.0))
        self.critical_cr = float(self.rules.get("critical_value_cr", 500.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            value_cr = row.get("traded_value")
            if value_cr is None or value_cr < self.min_value_cr:
                continue

            severity = ("critical" if value_cr >= self.critical_cr
                        else "notable")
            pct = row.get("pct_change")

            body_bits = [f"turnover Rs {value_cr:,.0f} cr"]
            if row.get("volume") is not None:
                body_bits.append(f"vol {row['volume']:,}")
            if pct is not None:
                body_bits.append(f"price {pct:+.2f}%")
            if row.get("last_price") is not None:
                body_bits.append(f"LTP {row['last_price']:,.2f}")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket="most_active",
                severity=severity,
                title=f"◆ HIGH TURNOVER {symbol} Rs {value_cr:,.0f} cr",
                body=" | ".join(body_bits),
                value=value_cr,
                dedup_key=make_dedup_key("equity_move", symbol),
                dedup_priority=0,
                payload={"symbol": symbol, "turnover_cr": value_cr,
                         "volume": row.get("volume"), "pct_change": pct},
            ))
        return signals
