"""Open interest spurts.

Endpoint: /api/live-analysis-oi-spurts-underlyings  (216 rows)

Units verified 2026-08-29 against a live sample:

  latestOI, prevOI, changeInOI   CONTRACTS. changeInOI == latestOI - prevOI
                                 held exactly on every row checked.

  avgInOI                        *** NOT AN AVERAGE ***
                                 It is the PERCENT change in OI. For
                                 ATHERENERG: (15621-5590)/5590*100 = 179.45,
                                 which is exactly the reported value. The
                                 name is misleading; treating it as an
                                 average OI level would make every threshold
                                 meaningless. This fetcher renames it to
                                 oi_change_pct so nothing downstream can
                                 repeat the mistake.

  futValue, premValue, total     LAKH. total == futValue + premValue held on
                                 all 20 rows checked, so the three share a
                                 unit; magnitude confirms lakh (ATHERENERG
                                 51,322 lakh = 513 cr of futures turnover).

  optValue                       RUPEES, and it is option *notional*, not
                                 premium. Median optValue/premValue ~5.8e6,
                                 far too large for a unit conversion of the
                                 same quantity.

  underlyingValue                rupees (spot price of the underlying)
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import (
    BaseFetcher, lakh_to_cr, parse_nse_date, rupees_to_cr, to_float, to_int,
)


class OiSpurtsFetcher(BaseFetcher):
    category = "oi_spurts"
    endpoint_name = "oi_spurts"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return (parse_nse_date(payload.get("currTradingDate"))
                    or parse_nse_date(payload.get("timestamp")))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        out: list[dict] = []
        for row in self.rows_from(payload):
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            latest_oi = to_int(row.get("latestOI"))
            prev_oi = to_int(row.get("prevOI"))
            change_oi = to_int(row.get("changeInOI"))

            # NSE calls the percent change "avgInOI". Recompute as a fallback
            # only; prefer their number so we match what the site displays.
            oi_change_pct = to_float(row.get("avgInOI"))
            if oi_change_pct is None and latest_oi is not None and prev_oi:
                oi_change_pct = (latest_oi - prev_oi) / prev_oi * 100.0

            out.append({
                "category": self.category,
                "bucket": "oi",
                "symbol": symbol,
                "last_price": to_float(row.get("underlyingValue")),
                "volume": to_int(row.get("volume")),
                # futValue is LAKH. Total F&O turnover for the underlying.
                "traded_value": lakh_to_cr(row.get("total")),
                "extra": {
                    "latest_oi": latest_oi,
                    "prev_oi": prev_oi,
                    "change_in_oi": change_oi,
                    # Deliberately NOT called avg_oi.
                    "oi_change_pct": oi_change_pct,
                    "fut_value_cr": lakh_to_cr(row.get("futValue")),
                    "prem_value_cr": lakh_to_cr(row.get("premValue")),
                    # optValue is RUPEES notional, a different unit again.
                    "opt_notional_cr": rupees_to_cr(row.get("optValue")),
                    "underlying_value": to_float(row.get("underlyingValue")),
                },
            })
        return out
