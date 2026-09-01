"""Top gainers / losers.

Endpoint: /api/live-analysis-variations?index=gainers|loosers
NSE spells the losers param "loosers"; that is in endpoints.yaml, not here.

The payload is keyed by index bucket (NIFTY, BANKNIFTY, NIFTYNEXT50, FOSec,
allSec, SecGtr20, SecLwr20) rather than being a flat list, so a symbol can
legitimately appear in several buckets at once.

Field notes from the live payload:
  ltp / prev_price / net_price / perChange   floats
  trade_quantity                             int
  turnover                                   INR lakh -- converted to crore
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, lakh_to_cr, parse_nse_date, to_float, to_int


class _VariationsFetcher(BaseFetcher):
    direction = ""          # "gainer" | "loser"

    def payload_trade_date(self, payload: Any) -> date | None:
        if not isinstance(payload, dict):
            return None
        for bucket in self.endpoint.buckets or []:
            node = payload.get(bucket)
            if isinstance(node, dict) and node.get("timestamp"):
                d = parse_nse_date(node["timestamp"])
                if d:
                    return d
        return None

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        buckets = self.endpoint.buckets or ["allSec"]
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []

        for bucket in buckets:
            node = payload.get(bucket)
            if not isinstance(node, dict):
                continue
            for row in node.get("data") or []:
                if not isinstance(row, dict):
                    continue
                symbol = (row.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                key = (bucket, symbol)
                if key in seen:
                    continue
                seen.add(key)

                out.append({
                    "category": self.category,
                    "bucket": bucket,
                    "symbol": symbol,
                    "company": (row.get("meta") or {}).get("companyName", "")
                    if isinstance(row.get("meta"), dict) else "",
                    "last_price": to_float(row.get("ltp")),
                    "prev_close": to_float(row.get("prev_price")),
                    "change": to_float(row.get("net_price")),
                    "pct_change": to_float(row.get("perChange")),
                    "volume": to_int(row.get("trade_quantity")),
                    "traded_value": lakh_to_cr(row.get("turnover")),
                    "extra": {
                        "series": row.get("series"),
                        "open": to_float(row.get("open_price")),
                        "high": to_float(row.get("high_price")),
                        "low": to_float(row.get("low_price")),
                        "direction": self.direction,
                        "ca_purpose": row.get("ca_purpose") or "",
                        "ca_ex_dt": row.get("ca_ex_dt") or "",
                    },
                })
        return out


class GainersFetcher(_VariationsFetcher):
    category = "gainers"
    endpoint_name = "gainers"
    direction = "gainer"


class LosersFetcher(_VariationsFetcher):
    category = "losers"
    endpoint_name = "losers"
    direction = "loser"
