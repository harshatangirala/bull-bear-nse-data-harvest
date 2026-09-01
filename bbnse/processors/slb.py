"""Importance rules for Securities Lending & Borrowing.

Only `spread_pct` (verified dimensionless, see fetchers/slb.py) and
`annualised_yield_pct` (already a percent by name and NSE convention) drive
this rule. `turnOver` / `transactionValue` are carried through in `extra`
for the reports but never compared against a threshold -- their unit could
not be confirmed against a live sample (every row had zero volume at
verification time).
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class SlbProcessor(BaseProcessor):
    category = "slb"
    config_key = "slb"
    rule_id = "slb_spread"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.spread_notable = float(self.rules.get("spread_pct_notable", 3.0))
        self.spread_critical = float(self.rules.get("spread_pct_critical", 6.0))
        self.yield_notable = float(self.rules.get("yield_pct_notable", 8.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            extra = row.get("extra") or {}
            spread_pct = extra.get("spread_pct")
            yield_pct = extra.get("annualised_yield_pct")
            if spread_pct is None and yield_pct is None:
                continue

            wide_spread = spread_pct is not None and abs(spread_pct) >= self.spread_notable
            high_yield = yield_pct is not None and yield_pct >= self.yield_notable
            if not (wide_spread or high_yield):
                continue

            severity = ("critical" if spread_pct is not None
                        and abs(spread_pct) >= self.spread_critical
                        else "notable")

            body_bits = []
            if spread_pct is not None:
                body_bits.append(f"spread {spread_pct:+.2f}%")
            if yield_pct is not None:
                body_bits.append(f"annualised yield {yield_pct:.2f}%")
            if extra.get("open_positions"):
                body_bits.append(f"OI {extra['open_positions']:,} contracts")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket="slb_wide" if wide_spread else "slb_high_yield",
                severity=severity,
                title=f"◆ SLB {symbol} " + ", ".join(body_bits[:1]),
                body=" | ".join(body_bits),
                value=spread_pct if spread_pct is not None else yield_pct,
                payload={"symbol": symbol, "spread_pct": spread_pct,
                         "annualised_yield_pct": yield_pct,
                         "series": extra.get("series")},
            ))
        return signals
