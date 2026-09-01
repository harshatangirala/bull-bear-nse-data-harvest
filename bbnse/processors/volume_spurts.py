"""Importance rules for volume spurts.

NSE's own list is already "unusual volume", so the job here is scale: how many
times its recent average is the stock trading, and is the rupee value big
enough for that to mean anything.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal
from .correlate import make_dedup_key


class VolumeSpurtsProcessor(BaseProcessor):
    category = "volume_spurts"
    config_key = "volume_spurts"
    rule_id = "volume_multiple"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.w1_mult = float(self.rules.get("volume_vs_week1_avg_multiple", 3.0))
        self.w2_mult = float(self.rules.get("volume_vs_week2_avg_multiple", 3.0))
        self.critical_mult = float(self.rules.get("critical_multiple", 6.0))
        self.min_value_cr = float(self.rules.get("min_traded_value_cr", 0.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            extra = row.get("extra") or {}
            m1 = extra.get("week1_multiple")
            m2 = extra.get("week2_multiple")
            if m1 is None and m2 is None:
                continue

            # Either window qualifying is enough; report the larger.
            hits = []
            if m1 is not None and m1 >= self.w1_mult:
                hits.append(("1-week", m1))
            if m2 is not None and m2 >= self.w2_mult:
                hits.append(("2-week", m2))
            if not hits:
                continue

            window, multiple = max(hits, key=lambda h: h[1])

            value_cr = row.get("traded_value")
            if self.min_value_cr > 0:
                if value_cr is None or value_cr < self.min_value_cr:
                    continue

            severity = ("critical" if multiple >= self.critical_mult
                        else "notable")
            pct = row.get("pct_change")

            body_bits = [f"{multiple:.1f}x its {window} average volume"]
            if row.get("volume") is not None:
                body_bits.append(f"vol {row['volume']:,}")
            if value_cr is not None:
                body_bits.append(f"turnover Rs {value_cr:,.0f} cr")
            if pct is not None:
                body_bits.append(f"price {pct:+.2f}%")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket="volume_spurt",
                severity=severity,
                title=f"◆ VOLUME SPURT {symbol} {multiple:.1f}x",
                body=" | ".join(body_bits),
                value=multiple,
                dedup_key=make_dedup_key("equity_move", symbol),
                # Below gainers: a price move is the more legible headline.
                dedup_priority=0,
                payload={
                    "symbol": symbol, "multiple": multiple, "window": window,
                    "volume": row.get("volume"), "turnover_cr": value_cr,
                    "pct_change": pct,
                },
            ))
        return signals
