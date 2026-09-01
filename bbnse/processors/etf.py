"""Importance rules for ETFs.

An ETF trading away from its NAV is the signal worth having: it means the
market price and the underlying basket have decoupled, which is either an
arbitrage or a liquidity problem. Plain price moves in an ETF are just the
underlying index moving and are already covered by the indices rule.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class EtfProcessor(BaseProcessor):
    category = "etf"
    config_key = "etf"
    rule_id = "nav_divergence"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.threshold = float(self.rules.get("premium_discount_pct", 1.5))
        self.critical = float(self.rules.get("critical_premium_pct",
                                             self.threshold * 2))
        self.min_value_cr = float(self.rules.get("min_traded_value_cr", 0.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            extra = row.get("extra") or {}
            premium = extra.get("premium_pct")
            if premium is None:
                continue

            magnitude = abs(premium)
            if magnitude < self.threshold:
                continue

            # A wide spread on an ETF nobody traded is a stale quote, not news.
            value_cr = row.get("traded_value")
            if self.min_value_cr > 0:
                if value_cr is None or value_cr < self.min_value_cr:
                    continue

            symbol = row.get("symbol") or ""
            severity = "critical" if magnitude >= self.critical else "notable"
            side = "premium" if premium > 0 else "discount"
            arrow = "▲" if premium > 0 else "▼"

            body_bits = [f"LTP {row.get('last_price'):,.2f} vs NAV "
                         f"{extra.get('nav'):,.2f}"
                         if row.get("last_price") is not None
                         and extra.get("nav") else side]
            if value_cr is not None:
                body_bits.append(f"turnover Rs {value_cr:,.1f} cr")
            if row.get("pct_change") is not None:
                body_bits.append(f"price {row['pct_change']:+.2f}%")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"etf_{side}",
                severity=severity,
                title=f"{arrow} ETF {symbol} {magnitude:.2f}% {side} to NAV",
                body=" | ".join(body_bits),
                value=premium,
                payload={"symbol": symbol, "premium_pct": premium,
                         "nav": extra.get("nav"),
                         "ltp": row.get("last_price"),
                         "turnover_cr": value_cr},
            ))
        return signals
