"""Price band hitters (upper / lower circuit).

Endpoint: /api/live-analysis-price-band-hitter

Payload shape: {upper|lower|both}.{AllSec|SecGtr20|SecLwr20}.data, with a
shared `count` block {TOTAL, UPPER, LOWER, BOTH}.

Units verified 2026-08-29 against a live sample -- and they differ from every
other equity feed:
  turnover         CRORE       (NOT lakh, unlike gainers/volume_spurts)
  totalTradedVol   LAKH shares (NOT absolute)
Cross-check: MASTEK 54.4906 lakh shares against 1002.872 turnover implies an
average price of 1,840.45 against an LTP of 1,933.90 (ratio 0.95) -- correct
for a stock that closed at its upper circuit. The same check held for five
rows at 0.95-0.98.

Also note `pChange` arrives space-padded ("  20.00") and prices arrive as
strings, so everything goes through to_float.
"""
from __future__ import annotations

from typing import Any

from .base import BaseFetcher, cr_to_cr, lakh_shares_to_shares, to_float


class PriceBandHittersFetcher(BaseFetcher):
    category = "price_band_hitters"
    endpoint_name = "price_band_hitters"

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for band in ("upper", "lower", "both"):
            node = payload.get(band)
            if not isinstance(node, dict):
                continue
            # AllSec is the superset; the Gtr20/Lwr20 splits are subsets of it.
            rows = (node.get("AllSec") or {}).get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = (row.get("symbol") or "").strip().upper()
                if not symbol or (band, symbol) in seen:
                    continue
                seen.add((band, symbol))

                out.append({
                    "category": self.category,
                    "bucket": band,
                    "symbol": symbol,
                    "last_price": to_float(row.get("ltp")),
                    "change": to_float(row.get("change")),
                    "pct_change": to_float(row.get("pChange")),
                    "volume": lakh_shares_to_shares(row.get("totalTradedVol")),
                    "traded_value": cr_to_cr(row.get("turnover")),  # CRORE
                    "extra": {
                        "band": band,
                        "series": row.get("series"),
                        "price_band_pct": to_float(row.get("priceBand")),
                        "high": to_float(row.get("highPrice")),
                        "low": to_float(row.get("lowPrice")),
                        "year_high": to_float(row.get("yearHigh")),
                        "year_low": to_float(row.get("yearLow")),
                    },
                })
        return out

    def band_counts(self, payload: Any) -> dict[str, int]:
        """Market-wide circuit counts, used by the daily report."""
        if not isinstance(payload, dict):
            return {}
        counts = (payload.get("upper", {}).get("AllSec", {}).get("count")
                  or payload.get("count") or {})
        return {k.lower(): int(to_float(v) or 0) for k, v in counts.items()}
