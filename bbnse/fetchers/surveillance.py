"""Surveillance actions: GSM, ASM, and price-band changes.

The "Price Bands & Surveillance Actions" landing page is a Drupal link hub,
not a data page -- it links out to three separate report pages, each with its
own endpoint:

  /reports/gsm                 -> /api/reportGSM
  /reports/asm                 -> /api/reportASM
  /reports/price-band-changes  -> /api/eqsurvactions

Discovery note: none of these turned up in the landing page's own script
tags. Each report page loads a page-specific bundle
(`/dist/js/sections/reports/{gsm,asm,price-band-changes}.js?v=...`) that an
early filename scan missed because it only matched literal `.js` suffixes --
every real script tag here carries a `?v=` cache-busting query string.
Widening the scan to every `<script src=...>` (not just ones ending `.js`)
found the page-specific bundles immediately.

All three payloads are purely categorical/date data -- symbol, stage,
description, an effective date -- with no money field anywhere, so there is
no unit ambiguity to resolve here (a rare break from the rest of this repo).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .base import BaseFetcher, parse_nse_date, to_float


class GsmFetcher(BaseFetcher):
    """Graded Surveillance Measure. Payload is a bare list."""
    category = "gsm"
    endpoint_name = "gsm"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, list) and payload:
            return parse_nse_date((payload[0] or {}).get("gsmTime"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        rows = payload if isinstance(payload, list) else []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            out.append({
                "category": self.category,
                "bucket": "gsm",
                "symbol": symbol,
                "company": row.get("companyName") or "",
                "extra": {
                    "gsm_stage": row.get("gsmStage") or "",
                    "surv_code": row.get("survCode") or "",
                    "surv_desc": row.get("survDesc") or "",
                    "isin": row.get("isin") or "",
                    "as_of": str(parse_nse_date(row.get("gsmTime")) or ""),
                },
            })
        return out


class AsmFetcher(BaseFetcher):
    """Additional Surveillance Measure. Payload is {longterm, shortterm}."""
    category = "asm"
    endpoint_name = "asm"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, dict):
            for bucket in ("longterm", "shortterm"):
                rows = (payload.get(bucket) or {}).get("data") or []
                if rows:
                    d = parse_nse_date(rows[0].get("asmTime"))
                    if d:
                        return d
        return None

    def normalize(self, payload: Any) -> list[dict]:
        if not isinstance(payload, dict):
            return []
        out: list[dict] = []
        for bucket in ("longterm", "shortterm"):
            rows = (payload.get(bucket) or {}).get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = (row.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                out.append({
                    "category": self.category,
                    "bucket": bucket,          # "longterm" | "shortterm"
                    "symbol": symbol,
                    "company": row.get("companyName") or "",
                    "extra": {
                        "asm_indicator": row.get("asmSurvIndicator") or "",
                        "surv_code": row.get("survCode") or "",
                        "surv_desc": row.get("survDesc") or "",
                        "isin": row.get("isin") or "",
                        "series": row.get("series"),
                        "term": bucket,
                        "as_of": str(parse_nse_date(row.get("asmTime")) or ""),
                    },
                })
        return out


class SurveillancePriceBandsFetcher(BaseFetcher):
    """Price bands newly tightened/relaxed by surveillance action.

    Payload is a bare list. `fromPriceBand` / `toPriceBand` are the percent
    band width as strings ("10" -> "5"), not prices -- there is nothing to
    unit-convert.
    """
    category = "surveillance_price_bands"
    endpoint_name = "surveillance_price_bands"

    def payload_trade_date(self, payload: Any) -> date | None:
        if isinstance(payload, list) and payload:
            return parse_nse_date((payload[0] or {}).get("effectiveDate"))
        return None

    def normalize(self, payload: Any) -> list[dict]:
        rows = payload if isinstance(payload, list) else []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            out.append({
                "category": self.category,
                "bucket": "band_change",
                "symbol": symbol,
                "company": row.get("secName") or "",
                "extra": {
                    "from_band_pct": to_float(row.get("fromPriceBand")),
                    "to_band_pct": to_float(row.get("toPriceBand")),
                    "effective_date": str(
                        parse_nse_date(row.get("effectiveDate")) or ""),
                },
            })
        return out
