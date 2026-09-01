"""Importance rules for institutional flows and report availability.

FII/DII net flow is a once-a-day number published after close, so there is no
intraday debounce concern -- but the state machine still keys on the trade
date, so re-running the daily job (or a catch-up backfill) will not re-alert.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class FiiDiiProcessor(BaseProcessor):
    category = "fii_dii"
    config_key = "fii_dii"
    rule_id = "net_flow"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.notable = float(self.rules.get("net_cr_notable", 2000.0))
        self.critical = float(self.rules.get("net_cr_critical", 5000.0))

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            extra = row.get("extra") or {}
            net = extra.get("net_cr")
            if net is None:
                continue

            magnitude = abs(net)
            if magnitude < self.notable:
                continue

            severity = "critical" if magnitude >= self.critical else "notable"
            buying = net > 0
            arrow = "▲" if buying else "▼"
            side = "net buying" if buying else "net selling"
            who = extra.get("investor_category") or row.get("symbol")

            body_bits = []
            if extra.get("buy_cr") is not None and extra.get("sell_cr") is not None:
                body_bits.append(f"bought Rs {extra['buy_cr']:,.0f} cr, "
                                 f"sold Rs {extra['sell_cr']:,.0f} cr")
            if extra.get("date"):
                body_bits.append(f"for {extra['date']}")

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=str(who).upper()[:32],
                # Keyed on the date so a catch-up backfill cannot re-alert an
                # already-reported session.
                state_bucket=f"flow_{extra.get('date', '')}",
                severity=severity,
                title=f"{arrow} {who} {side} Rs {magnitude:,.0f} cr",
                body=" | ".join(body_bits),
                value=net,
                payload={"investor_category": who, "net_cr": net,
                         "buy_cr": extra.get("buy_cr"),
                         "sell_cr": extra.get("sell_cr"),
                         "date": extra.get("date")},
            ))
        return signals


class DailyReportsProcessor(BaseProcessor):
    category = "daily_reports"
    config_key = "daily_reports"
    rule_id = "report_published"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.severity = self.rules.get("severity", "info")
        self.watch = [w.strip().lower() for w in
                      (self.rules.get("watch_reports") or []) if str(w).strip()]

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            extra = row.get("extra") or {}
            name = extra.get("report_name") or ""
            if not name:
                continue
            # Empty watch list means every published report is reported once.
            if self.watch and not any(w in name.lower() for w in self.watch):
                continue

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=row.get("symbol") or name.upper()[:32],
                state_bucket="published",
                severity=self.severity,
                title=f"▣ REPORT PUBLISHED {name}",
                body=extra.get("link") or "",
                payload={"report_name": name, "link": extra.get("link")},
            ))
        return signals
