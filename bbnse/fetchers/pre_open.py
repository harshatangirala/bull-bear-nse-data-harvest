"""Pre-open session (09:00-09:15).

Endpoints: /api/market-data-pre-open?key=ALL   (cash market, 2072 rows, 2 MB)
           /api/market-data-pre-open?key=FO    (F&O universe, 210 rows)

Units verified 2026-08-29 against a live sample:
  metadata.totalTurnover   RUPEES -- finalQuantity x lastPrice reproduced it
                           exactly on four rows (ratio 1.000), e.g. LANCORHOL
                           202,763 x 33.00 = 6,691,179 against 6,691,179
  metadata.finalQuantity   share count (absolute)
  metadata.iep             indicative equilibrium price, rupees
Top-level advances/declines/unchanged are plain counts.

Rows are nested one level deeper than every other feed: data[].metadata holds
the quote, data[].detail holds the order book.

Persistence note: this is the heaviest feed in the project. All 2072 rows are
normalized so the processor sees everything, but only rows moving at least
`rules.pre_open.persist_min_pct_move` are written to the observation table --
every row has finalQuantity > 0, so that filter would save nothing, while the
move filter cuts 2072 rows to ~200. The full payload is still in the raw
snapshot either way.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, rupees_to_cr, to_float, to_int


class _PreOpenFetcher(BaseFetcher):
    segment = "CM"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        out: list[dict] = []
        for row in payload.get("data") or []:
            if not isinstance(row, dict):
                continue
            meta = row.get("metadata")
            if not isinstance(meta, dict):
                continue
            symbol = (meta.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            out.append({
                "category": self.category,
                "bucket": self.segment,
                "symbol": symbol,
                "last_price": to_float(meta.get("lastPrice")),
                "prev_close": to_float(meta.get("previousClose")),
                "change": to_float(meta.get("change")),
                "pct_change": to_float(meta.get("pChange")),
                "volume": to_int(meta.get("finalQuantity")),
                # RUPEES -> crore. Verified; not lakh like the gainers feed.
                "traded_value": rupees_to_cr(meta.get("totalTurnover")),
                "extra": {
                    "series": meta.get("series"),
                    "iep": to_float(meta.get("iep")),
                    "year_high": to_float(meta.get("yearHigh")),
                    "year_low": to_float(meta.get("yearLow")),
                    "segment": self.segment,
                },
            })
        return out

    def market_totals(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        return {
            "advances": to_int(payload.get("advances")),
            "declines": to_int(payload.get("declines")),
            "unchanged": to_int(payload.get("unchanged")),
            "total_traded_value_cr": rupees_to_cr(
                payload.get("totalTradedValue")),
            "total_traded_volume": to_int(payload.get("totalTradedVolume")),
        }

    def persist(self, rows: list[dict], trade_date: date | None) -> None:
        floor = float(self.cfg.get("rules.pre_open.persist_min_pct_move", 2.0))
        if floor > 0:
            rows = [r for r in rows
                    if r.get("pct_change") is not None
                    and abs(r["pct_change"]) >= floor]
        super().persist(rows, trade_date)


class PreOpenCashFetcher(_PreOpenFetcher):
    category = "pre_open_cm"
    endpoint_name = "pre_open_cm"
    segment = "CM"


class PreOpenFnoFetcher(_PreOpenFetcher):
    category = "pre_open_fo"
    endpoint_name = "pre_open_fo"
    segment = "FO"
