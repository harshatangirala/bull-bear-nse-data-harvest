"""Daily report -- generated after market close.

Regenerable for any past date from the normalized tables:
    python main.py report daily --date 2026-08-28
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from ..core.logging_setup import get_logger
from ..storage.dao import Dao
from .base import ReportBuilder, ReportOutput, Section, to_local

log = get_logger(__name__)

SEV_RANK = {"critical": 0, "notable": 1, "info": 2}


def _latest_per_symbol(observations, category: str) -> list:
    """Last observation of the day per symbol -- the closing state."""
    latest: dict[str, object] = {}
    for obs in observations:
        if obs.category != category:
            continue
        cur = latest.get(obs.symbol)
        if cur is None or obs.observed_at >= cur.observed_at:
            latest[obs.symbol] = obs
    return list(latest.values())


class DailyReport:
    slug = "daily"

    def __init__(self, cfg, dao: Dao):
        self.cfg = cfg
        self.dao = dao

    def generate(self, trade_date: date, *, write: bool = True) -> ReportOutput:
        observations = self.dao.observations_for_date(trade_date)
        deals = self.dao.deals_for_date(trade_date)
        alerts = self.dao.alerts_for_date(trade_date)

        title = f"NSE Daily Report — {trade_date.strftime('%A, %d %b %Y')}"
        sev_counts = Counter(a.severity for a in alerts)
        subtitle = (
            f"{len(alerts)} alerts "
            f"({sev_counts.get('critical', 0)} critical, "
            f"{sev_counts.get('notable', 0)} notable) · "
            f"{len(observations):,} observations · {len(deals)} deals recorded"
        )
        rb = ReportBuilder(self.cfg, title, subtitle)

        # --- 1. at a glance --------------------------------------------------
        by_cat = Counter(a.category for a in alerts)
        highs = _latest_per_symbol(observations, "week52_high")
        lows = _latest_per_symbol(observations, "week52_low")
        bulk = [d for d in deals if d.deal_type == "BULK"]
        block = [d for d in deals if d.deal_type == "BLOCK"]
        total_deal_cr = sum(d.value_cr or 0 for d in deals)

        rb.add(Section(
            heading="At a glance",
            columns=["Metric", "Value"],
            numeric_cols={1},
            rows=[
                ["Alerts fired", len(alerts)],
                ["  critical", sev_counts.get("critical", 0)],
                ["  notable", sev_counts.get("notable", 0)],
                ["Fresh 52-week highs", len(highs)],
                ["Fresh 52-week lows", len(lows)],
                ["Bulk deals", len(bulk)],
                ["Block deals", len(block)],
                ["Total deal value (Rs cr)", round(total_deal_cr, 1)],
            ],
        ))

        # --- 2. market breadth proxy from the 52w feeds ---------------------
        if highs or lows:
            ratio = (len(highs) / len(lows)) if lows else float(len(highs))
            tone = ("decisively bullish" if ratio >= 3
                    else "bullish" if ratio > 1.2
                    else "decisively bearish" if ratio <= 0.33
                    else "bearish" if ratio < 0.8 else "balanced")
            rb.add(Section(
                heading="52-week extremes breadth",
                note=(f"{len(highs)} new highs vs {len(lows)} new lows "
                      f"(ratio {ratio:.2f}) — {tone}."),
            ))

        # --- 3. alerts fired -------------------------------------------------
        alert_rows = []
        for a in sorted(alerts, key=lambda x: (SEV_RANK.get(x.severity, 9),
                                               x.created_at)):
            alert_rows.append([
                to_local(a.created_at).strftime("%H:%M:%S"), a.severity.upper(),
                a.category, a.entity, a.title,
                "" if a.kind == "new" else a.kind,
            ])
        rb.add(Section(
            heading="Alerts fired today",
            columns=["Time", "Severity", "Category", "Symbol", "Event", "Kind"],
            rows=alert_rows,
            empty_text="No alerts crossed threshold today.",
        ))

        # --- 4. top movers ---------------------------------------------------
        for category, label in (("gainers", "Top gainers"),
                                ("losers", "Top losers")):
            rows = _latest_per_symbol(observations, category)
            rows.sort(key=lambda o: abs(o.pct_change or 0), reverse=True)
            rb.add(Section(
                heading=label,
                columns=["Symbol", "LTP", "% Change", "Turnover (cr)",
                         "Volume", "Bucket"],
                numeric_cols={1, 2, 3, 4},
                rows=[[o.symbol, o.last_price, o.pct_change, o.traded_value,
                       o.volume, o.bucket] for o in rows[:15]],
                empty_text=f"No {category} data captured for this date.",
            ))

        # --- 5. 52-week extremes --------------------------------------------
        for rows, label, kind in ((highs, "New 52-week highs", "high"),
                                  (lows, "New 52-week lows", "low")):
            rows = sorted(rows, key=lambda o: abs(o.pct_change or 0),
                          reverse=True)
            rb.add(Section(
                heading=label,
                columns=["Symbol", "Company", "LTP", f"New {kind}",
                         f"Prev {kind}", "Prev date", "Day %"],
                numeric_cols={2, 3, 4, 6},
                rows=[[o.symbol, (o.company or "")[:38], o.last_price,
                       o.extreme_value, o.prev_extreme,
                       o.prev_extreme_date or "–", o.pct_change]
                      for o in rows[:20]],
                empty_text=f"No new 52-week {kind}s recorded.",
            ))

        # --- 6. large deals --------------------------------------------------
        for dtype, label in (("BULK", "Bulk deals"), ("BLOCK", "Block deals"),
                             ("SHORT", "Short deals")):
            subset = [d for d in deals if d.deal_type == dtype]
            subset.sort(key=lambda d: d.value_cr or 0, reverse=True)
            rb.add(Section(
                heading=f"{label} (top by value)",
                columns=["Symbol", "Client", "Side", "Qty", "Price",
                         "Value (cr)"],
                numeric_cols={3, 4, 5},
                rows=[[d.symbol, (d.client_name or "")[:42], d.buy_sell,
                       d.quantity, d.price, round(d.value_cr, 2)
                       if d.value_cr is not None else None]
                      for d in subset[:15]],
                empty_text=f"No {label.lower()} recorded for this date.",
            ))

        # --- 7. most-alerted symbols ----------------------------------------
        if alerts:
            per_symbol = defaultdict(list)
            for a in alerts:
                per_symbol[a.entity].append(a)
            busiest = sorted(per_symbol.items(), key=lambda kv: len(kv[1]),
                             reverse=True)[:10]
            rb.add(Section(
                heading="Most-alerted symbols",
                columns=["Symbol", "Alerts", "Categories"],
                numeric_cols={1},
                rows=[[sym, len(items),
                       ", ".join(sorted({i.category for i in items}))]
                      for sym, items in busiest],
            ))

        # --- 8. pipeline health ---------------------------------------------
        health = self.dao.all_health()
        rb.add(Section(
            heading="Pipeline health",
            columns=["Category", "Last success", "Rows", "Consec. failures",
                     "Last error"],
            numeric_cols={2, 3},
            rows=[[h.category,
                   to_local(h.last_success_at).strftime("%d-%b %H:%M")
                   if h.last_success_at else "never",
                   h.last_row_count, h.consecutive_failures,
                   (h.last_error or "")[:60]]
                  for h in sorted(health, key=lambda x: x.category)],
            empty_text="No fetcher activity recorded.",
        ))

        summary = self._summary(trade_date, alerts, sev_counts, by_cat,
                                highs, lows, deals)
        md_path = html_path = None
        if write:
            md_path, html_path = rb.write(self.slug, trade_date)

        return ReportOutput(title=title, markdown=rb.to_markdown(),
                            summary=summary, md_path=md_path,
                            html_path=html_path)

    @staticmethod
    def _summary(trade_date, alerts, sev_counts, by_cat, highs, lows,
                 deals) -> str:
        """Short scannable digest for the Telegram message body."""
        lines = [f"NSE Daily — {trade_date.strftime('%d %b %Y')}", ""]
        lines.append(f"Alerts: {len(alerts)} "
                     f"({sev_counts.get('critical', 0)} critical, "
                     f"{sev_counts.get('notable', 0)} notable)")
        if by_cat:
            lines.append("By category: " + ", ".join(
                f"{c} {n}" for c, n in by_cat.most_common()))
        lines.append(f"52w highs {len(highs)} · 52w lows {len(lows)}")
        # A block deal above the bulk threshold is published in both feeds, so
        # the same trade appears twice in deal_observation. The per-type tables
        # above want both rows; this cross-type digest must not repeat itself.
        seen: set[tuple] = set()
        big = []
        for d in sorted((x for x in deals if x.value_cr),
                        key=lambda x: -x.value_cr):
            key = (d.symbol, (d.client_name or "").upper(), d.buy_sell,
                   d.quantity, d.price)
            if key in seen:
                continue
            seen.add(key)
            big.append(d)

        if big:
            lines.append("")
            lines.append("Biggest deals:")
            for d in big[:5]:
                lines.append(f"  {d.symbol} {d.buy_sell} Rs {d.value_cr:,.0f} cr"
                             f" — {(d.client_name or '')[:32]}")
        return "\n".join(lines)
