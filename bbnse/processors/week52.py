"""Importance rules for 52-week highs / lows.

A fresh 52-week extreme is inherently notable, so the job here is mostly
noise control: drop penny stocks, drop extremes that barely clear the old
one, and escalate breakouts that clear it by a wide margin.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class Week52Processor(BaseProcessor):
    config_key = "week52"
    rule_id = "fresh_extreme"

    def __init__(self, cfg, universe=None, category: str = "week52_high"):
        self.category = category
        super().__init__(cfg, universe)
        self.kind = "high" if category.endswith("high") else "low"
        self.min_ltp = float(self.rules.get("min_ltp", 0.0))
        self.min_margin = float(self.rules.get("min_margin_pct", 0.0))
        self.breakout_critical = float(
            self.rules.get("breakout_margin_pct_critical", 3.0)
        )
        self.base_severity = self.rules.get(
            f"severity_{self.kind}", "notable"
        )

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            ltp = row.get("last_price")
            if self.min_ltp > 0 and (ltp is None or ltp < self.min_ltp):
                continue

            margin = (row.get("extra") or {}).get("margin_pct")
            # margin_pct is signed so positive always means "more extreme".
            if margin is not None and margin < self.min_margin:
                continue

            severity = self.base_severity
            if margin is not None and margin >= self.breakout_critical:
                severity = "critical"

            new_extreme = row.get("extreme_value")
            prev_extreme = row.get("prev_extreme")
            prev_date = row.get("prev_extreme_date") or ""
            pct = row.get("pct_change")

            label = "52W HIGH" if self.kind == "high" else "52W LOW"
            arrow = "▲" if self.kind == "high" else "▼"

            body_bits = []
            if new_extreme is not None:
                body_bits.append(f"new {self.kind} {new_extreme:,.2f}")
            if prev_extreme is not None:
                prev_txt = f"prev {prev_extreme:,.2f}"
                if prev_date:
                    prev_txt += f" ({prev_date})"
                body_bits.append(prev_txt)
            if margin is not None:
                body_bits.append(f"clears by {margin:+.2f}%")
            if pct is not None:
                body_bits.append(f"day {pct:+.2f}%")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"week52_{self.kind}",
                severity=severity,
                title=f"{arrow} {label} {symbol} @ "
                      + (f"{new_extreme:,.2f}" if new_extreme is not None
                         else "n/a"),
                body=" | ".join(body_bits),
                # Track the extreme itself so escalation fires when the stock
                # keeps making new highs, not on every intraday wiggle.
                value=new_extreme,
                payload={
                    "symbol": symbol, "kind": self.kind,
                    "new_extreme": new_extreme, "prev_extreme": prev_extreme,
                    "prev_extreme_date": prev_date, "ltp": ltp,
                    "margin_pct": margin, "pct_change": pct,
                    "company": row.get("company", ""),
                },
            ))
        return signals
