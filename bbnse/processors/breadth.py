"""Importance rules for market breadth.

The entity here is the market, not a symbol, so the universe filter does not
apply and the state bucket encodes which extreme was crossed. Breadth swings
around during the day, so alerting on every crossing would be noise -- the
state machine's transition-only behaviour is doing the heavy lifting.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal
from ..fetchers.breadth import BREADTH_ENTITY


class AdvanceDeclineProcessor(BaseProcessor):
    category = "advance_decline"
    config_key = "advance_decline"
    rule_id = "breadth_extreme"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.bullish = float(self.rules.get("ratio_extreme_bullish", 3.0))
        self.bearish = float(self.rules.get("ratio_extreme_bearish", 0.33))
        self.min_total = int(self.rules.get("min_total", 100))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            extra = row.get("extra") or {}
            advances = extra.get("advances") or 0
            declines = extra.get("declines") or 0
            total = extra.get("total") or 0
            ratio = extra.get("ad_ratio")

            # Early in the session the counts are too small to mean anything.
            if total < self.min_total or ratio is None:
                continue

            if ratio >= self.bullish:
                direction, arrow = "bullish", "▲"
            elif ratio <= self.bearish:
                direction, arrow = "bearish", "▼"
            else:
                continue

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=BREADTH_ENTITY,
                state_bucket=f"breadth_{direction}",
                severity="notable",
                title=(f"{arrow} MARKET BREADTH extreme {direction} "
                       f"({ratio:.2f}:1)"),
                body=(f"{advances:,} advancing vs {declines:,} declining "
                      f"of {total:,} traded"),
                value=ratio,
                payload={"advances": advances, "declines": declines,
                         "unchanged": extra.get("unchanged"),
                         "total": total, "ad_ratio": ratio,
                         "direction": direction},
            ))
        return signals
