"""Institutional flows (FII/FPI and DII) and the daily report index.

Endpoints: /api/fiidiiTradeReact
           /api/daily-reports?key=favCapital

FII/DII units verified 2026-08-29 against a live sample:
  buyValue, sellValue, netValue   CRORE, delivered as STRINGS.
  Confirmed by magnitude and internal consistency: DII buy 16,539.38 minus
  sell 11,355.45 equals net 5,183.93 exactly, and figures of that size are
  crore for a single session (they would be absurd as rupees or as lakh).
  This is the only feed in the project that is already in the target unit,
  which is precisely why it goes through cr_to_cr() rather than being passed
  through silently.

The payload is a bare LIST, not a dict -- one row per investor category.

daily_reports returns {"data": [], "msg": "no data found"} outside publishing
hours; the row shape is not exercised here beyond name/link extraction.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, cr_to_cr, parse_nse_date


class FiiDiiFetcher(BaseFetcher):
    category = "fii_dii"
    endpoint_name = "fii_dii"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, list) and payload:
            return parse_nse_date((payload[0] or {}).get("date"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        # Bare list, unlike almost every other endpoint.
        rows = payload if isinstance(payload, list) else self.rows_from(payload)
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            category = (row.get("category") or "").strip()
            if not category:
                continue

            buy = cr_to_cr(row.get("buyValue"))       # already CRORE
            sell = cr_to_cr(row.get("sellValue"))
            net = cr_to_cr(row.get("netValue"))
            if net is None and buy is not None and sell is not None:
                net = buy - sell

            out.append({
                "category": self.category,
                "bucket": "flow",
                # "FII/FPI" contains a slash; keep it as the entity verbatim
                # so it matches what NSE publishes.
                "symbol": category.upper()[:32],
                "company": category,
                "traded_value": net,
                "extra": {
                    "investor_category": category,
                    "buy_cr": buy,
                    "sell_cr": sell,
                    "net_cr": net,
                    "date": str(parse_nse_date(row.get("date")) or ""),
                },
            })
        return out


class DailyReportsFetcher(BaseFetcher):
    category = "daily_reports"
    endpoint_name = "daily_reports"

    def normalize(self, payload: Any) -> list[dict]:
        rows = self.rows_from(payload)
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = (row.get("name") or row.get("fileName")
                    or row.get("reportName") or "").strip()
            if not name:
                continue
            out.append({
                "category": self.category,
                "bucket": "report",
                "symbol": name.upper()[:32],
                "company": name,
                "extra": {
                    "report_name": name,
                    "link": row.get("link") or row.get("filePath") or "",
                    "section": row.get("section") or "",
                },
            })
        return out
