"""Importance rules for the pre-open session.

Pre-open prices are discovered by auction, not continuous trading, so a large
pre-open move is a genuine signal about where the stock will open -- and it is
information you can only act on before 09:15. Thresholds are deliberately
looser than the intraday ones because pre-open gaps are larger by nature.

The `min_value_cr` floor matters more here than intraday: pre-open volumes are
thin, so a 15% move on a few thousand rupees of turnover is meaningless.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class PreOpenProcessor(BaseProcessor):
    config_key = "pre_open"
    rule_id = "pre_open_gap"

    def __init__(self, cfg, universe=None, category: str = "pre_open_cm"):
        self.category = category
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("pct_move_notable", 4.0))
        self.critical = float(self.rules.get("pct_move_critical", 8.0))
        self.min_value_cr = float(self.rules.get("min_value_cr", 1.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not self.in_universe(symbol):
                continue

            pct = row.get("pct_change")
            if pct is None:
                continue
            magnitude = abs(pct)
            if magnitude < self.notable:
                continue

            # Pre-open books are thin; ignore gaps on negligible turnover.
            value_cr = row.get("traded_value")
            if self.min_value_cr > 0:
                if value_cr is None or value_cr < self.min_value_cr:
                    continue

            severity = "critical" if magnitude >= self.critical else "notable"
            arrow = "▲" if pct > 0 else "▼"
            direction = "gap up" if pct > 0 else "gap down"
            extra = row.get("extra") or {}

            body_bits = []
            if extra.get("iep") is not None:
                body_bits.append(f"IEP {extra['iep']:,.2f}")
            if row.get("prev_close") is not None:
                body_bits.append(f"prev close {row['prev_close']:,.2f}")
            if value_cr is not None:
                body_bits.append(f"pre-open turnover Rs {value_cr:,.2f} cr")
            body_bits.append(extra.get("segment") or "")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"preopen_{'up' if pct > 0 else 'down'}",
                severity=severity,
                title=f"{arrow} PRE-OPEN {direction} {symbol} {pct:+.2f}%",
                body=" | ".join(b for b in body_bits if b),
                value=pct,
                payload={"symbol": symbol, "pct_change": pct,
                         "iep": extra.get("iep"),
                         "prev_close": row.get("prev_close"),
                         "turnover_cr": value_cr,
                         "segment": extra.get("segment")},
            ))
        return signals
