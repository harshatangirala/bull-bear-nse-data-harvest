"""Importance rules for index moves.

An index moving 2% is a market event, not a stock event, so this rule uses
much tighter thresholds than the equity ones and ignores the alert universe
entirely (index names are not in it). Only the indices named in
`rules.indices.watch` are evaluated -- there are 139 of them and most are
narrow sector slices that move together.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class IndicesProcessor(BaseProcessor):
    category = "indices_all"
    config_key = "indices"
    rule_id = "index_move"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("pct_move_notable", 1.0))
        self.critical = float(self.rules.get("pct_move_critical", 2.0))
        self.watch = {w.strip().upper() for w in
                      (self.rules.get("watch") or []) if str(w).strip()}

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            name = (row.get("company") or row.get("symbol") or "").strip()
            symbol = (row.get("symbol") or "").strip().upper()
            # Match on either the display name or the symbol, since NSE uses
            # both spellings across its feeds ("NIFTY 50" / "NIFTY50").
            if self.watch and not (
                    symbol in self.watch or name.upper() in self.watch):
                continue

            pct = row.get("pct_change")
            if pct is None or abs(pct) < self.notable:
                continue

            magnitude = abs(pct)
            severity = "critical" if magnitude >= self.critical else "notable"
            arrow = "▲" if pct > 0 else "▼"
            last = row.get("last_price")
            change = row.get("change")

            body_bits = []
            if last is not None:
                body_bits.append(f"at {last:,.2f}")
            if change is not None:
                body_bits.append(f"{change:+,.2f} pts")
            extra = row.get("extra") or {}
            if extra.get("high") and extra.get("low"):
                body_bits.append(f"range {extra['low']:,.0f}–{extra['high']:,.0f}")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"index_{'up' if pct > 0 else 'down'}",
                severity=severity,
                title=f"{arrow} {name} {pct:+.2f}%",
                body=" | ".join(body_bits),
                value=pct,
                payload={"index": name, "symbol": symbol, "pct_change": pct,
                         "last": last, "change_points": change},
            ))
        return signals
