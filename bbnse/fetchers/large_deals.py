"""Large deals -- bulk, block and short deals.

Endpoint: /api/snapshot-capital-market-largedeal

One payload carries three independent blocks, so this fetcher emits rows
tagged by deal_type rather than one flat list:
  BULK_DEALS_DATA   >0.5% of listed shares traded by one client in a day
  BLOCK_DEALS_DATA  pre-negotiated single trades in the block window
  SHORT_DEALS_DATA  disclosed short positions

Field notes: qty and watp arrive as strings, remarks can be null, and the
deal date is per-row ("28-Aug-2026") with an `as_on_date` at the top level.

These rows go to DealObservation with a stable dedupe_key: the endpoint is
polled repeatedly after close and returns the same deals each time.
"""
from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float, to_int

# root key in payload -> deal_type tag
_BLOCKS = {
    "BULK_DEALS_DATA": "BULK",
    "BLOCK_DEALS_DATA": "BLOCK",
    "SHORT_DEALS_DATA": "SHORT",
}


def _dedupe_key(deal_type: str, trade_date: date | None, symbol: str,
                client: str, buy_sell: str, qty: int | None,
                price: float | None) -> str:
    parts = "|".join([
        deal_type, trade_date.isoformat() if trade_date else "",
        symbol, client.upper(), buy_sell.upper(),
        str(qty or ""), f"{price:.4f}" if price is not None else "",
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:48]


class LargeDealsFetcher(BaseFetcher):
    category = "large_deals"
    endpoint_name = "large_deals"
    # Deals have their own table; skip the generic observation writer.
    persists_observations = False

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            return parse_nse_date(payload.get("as_on_date"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        fallback_date = self.payload_trade_date(payload)
        out: list[dict] = []

        for root, deal_type in _BLOCKS.items():
            for row in payload.get(root) or []:
                if not isinstance(row, dict):
                    continue
                symbol = (row.get("symbol") or "").strip().upper()
                if not symbol:
                    continue

                qty = to_int(row.get("qty"))
                price = to_float(row.get("watp"))
                trade_date = parse_nse_date(row.get("date")) or fallback_date
                client = (row.get("clientName") or "").strip()
                buy_sell = (row.get("buySell") or "").strip().upper()

                # Deal value in crore; both inputs are strings in the feed.
                value_cr = None
                if qty is not None and price is not None:
                    value_cr = (qty * price) / 1e7

                out.append({
                    "deal_type": deal_type,
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "company": (row.get("name") or "").strip(),
                    "client_name": client,
                    "buy_sell": buy_sell,
                    "quantity": qty,
                    "price": price,
                    "value_cr": value_cr,
                    # remarks is sometimes null, sometimes "-"
                    "remarks": (row.get("remarks") or "").strip(),
                    "dedupe_key": _dedupe_key(deal_type, trade_date, symbol,
                                              client, buy_sell, qty, price),
                })
        return out

    def persist(self, rows: list[dict], trade_date: date | None) -> None:
        if not rows:
            return
        inserted = self.dao.add_deals(rows)
        if inserted:
            from ..core.logging_setup import get_logger
            get_logger(__name__).info(
                "new deals stored",
                extra={"inserted": inserted, "seen": len(rows)},
            )
