"""Importance rules for price band (circuit) hitters.

A stock locked at its circuit cannot be traded out of, so this is the one
category that defaults to `critical`: it is a liquidity event, not just a
price move.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal
from .correlate import make_dedup_key


class PriceBandProcessor(BaseProcessor):
    category = "price_band_hitters"
    config_key = "price_band"
    rule_id = "band_hit"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.alert_on = {b.lower() for b in
                         (self.rules.get("alert_on") or ["upper", "lower"])}
        self.severity = self.rules.get("severity", "critical")
        self.min_ltp = float(self.rules.get("min_ltp", 0.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            band = (row.get("bucket") or "").lower()
            if band not in self.alert_on:
                continue

            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            ltp = row.get("last_price")
            if self.min_ltp > 0 and (ltp is None or ltp < self.min_ltp):
                continue

            extra = row.get("extra") or {}
            band_pct = extra.get("price_band_pct")
            pct = row.get("pct_change")
            value_cr = row.get("traded_value")

            arrow = "▲" if band == "upper" else "▼"
            label = f"{band.upper()} CIRCUIT"

            body_bits = []
            if band_pct is not None:
                body_bits.append(f"{band_pct:.0f}% band")
            if pct is not None:
                body_bits.append(f"{pct:+.2f}% today")
            if ltp is not None:
                body_bits.append(f"LTP {ltp:,.2f}")
            if value_cr is not None:
                body_bits.append(f"turnover Rs {value_cr:,.1f} cr")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                # Separate states per band, so a stock that swings from upper
                # to lower circuit alerts on both.
                state_bucket=f"band_{band}",
                severity=self.severity,
                title=f"{arrow} {label} {symbol}",
                body=" | ".join(body_bits),
                value=pct,
                dedup_key=make_dedup_key("equity_move", symbol),
                # Highest in the equity group: being circuit-locked outranks
                # simply being a big mover, and its severity lets it break
                # through as an escalation if gainers alerted first.
                dedup_priority=2,
                payload={
                    "symbol": symbol, "band": band, "price_band_pct": band_pct,
                    "ltp": ltp, "pct_change": pct, "turnover_cr": value_cr,
                },
            ))
        return signals
