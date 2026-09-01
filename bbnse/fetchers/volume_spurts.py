"""Volume gainers / spurts.

Endpoint: /api/live-analysis-volume-gainers

Units verified 2026-08-29 against a live sample:
  volume, week1AvgVolume, week2AvgVolume   share counts (absolute)
  turnover                                 LAKH  -- median(volume*ltp/turnover)
                                           = 100,960 across 25 rows
  week1volChange                           already a MULTIPLE, not a percent:
                                           volume/week1AvgVolume reproduced it
                                           exactly (6.9103 for 1289999/186676)

That last one matters: treating `week1volChange` as a percentage would make a
3x spurt look like a 3% one and nothing would ever alert.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, lakh_to_cr, parse_nse_date, to_float, to_int


class VolumeSpurtsFetcher(BaseFetcher):
    category = "volume_spurts"
    endpoint_name = "volume_spurts"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("timestamp"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        out: list[dict] = []
        for row in self.rows_from(payload):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            volume = to_int(row.get("volume"))
            w1 = to_int(row.get("week1AvgVolume"))
            w2 = to_int(row.get("week2AvgVolume"))

            # NSE supplies the multiple already; recompute as a fallback only.
            w1_mult = to_float(row.get("week1volChange"))
            if w1_mult is None and volume and w1:
                w1_mult = volume / w1
            w2_mult = to_float(row.get("week2volChange"))
            if w2_mult is None and volume and w2:
                w2_mult = volume / w2

            out.append({
                "category": self.category,
                "bucket": "spurt",
                "symbol": symbol,
                "company": row.get("companyName") or "",
                "last_price": to_float(row.get("ltp")),
                "pct_change": to_float(row.get("pChange")),
                "volume": volume,
                "traded_value": lakh_to_cr(row.get("turnover")),   # LAKH -> cr
                "extra": {
                    "week1_avg_volume": w1,
                    "week2_avg_volume": w2,
                    "week1_multiple": w1_mult,
                    "week2_multiple": w2_mult,
                },
            })
        return out
