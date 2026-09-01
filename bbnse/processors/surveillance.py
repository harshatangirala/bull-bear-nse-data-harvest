"""Importance rules for GSM, ASM, and surveillance price-band changes.

All three are inherently discrete events -- a security either is or is not
newly subject to a surveillance measure -- so there is no threshold to tune.
The state machine's fire-once-on-transition behaviour does all the real
work: the same security sitting in the same GSM stage across consecutive
polls stays silent, but a stage change (e.g. GSM I -> GSM II, or dropping
off the list entirely as a new state_bucket) fires again.
"""
from __future__ import annotations

from .base import BaseProcessor, Signal


class GsmProcessor(BaseProcessor):
    category = "gsm"
    config_key = "gsm"
    rule_id = "gsm_stage"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.severity = self.rules.get("severity", "notable")

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not symbol:
                continue
            extra = row.get("extra") or {}
            stage = extra.get("gsm_stage") or ""

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                # Keyed on the stage itself: moving between stages re-alerts.
                state_bucket=f"gsm_{stage}",
                severity=self.severity,
                title=f"⛔ GSM {symbol} stage {stage}",
                body=extra.get("surv_desc") or extra.get("surv_code") or "",
                payload={"symbol": symbol, "gsm_stage": stage,
                         "surv_code": extra.get("surv_code")},
            ))
        return signals


class AsmProcessor(BaseProcessor):
    category = "asm"
    config_key = "asm"
    rule_id = "asm_stage"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.severity = self.rules.get("severity", "notable")
        self.terms = set(self.rules.get("terms") or ["longterm", "shortterm"])

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not symbol:
                continue
            extra = row.get("extra") or {}
            term = extra.get("term") or row.get("bucket") or ""
            if term not in self.terms:
                continue
            indicator = extra.get("asm_indicator") or ""

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                state_bucket=f"asm_{term}_{indicator}",
                severity=self.severity,
                title=f"⛔ ASM ({term}) {symbol} {indicator}",
                body=extra.get("surv_desc") or extra.get("surv_code") or "",
                payload={"symbol": symbol, "term": term,
                         "asm_indicator": indicator,
                         "surv_code": extra.get("surv_code")},
            ))
        return signals


class SurveillancePriceBandsProcessor(BaseProcessor):
    category = "surveillance_price_bands"
    config_key = "surveillance_price_bands"
    rule_id = "band_revision"

    def __init__(self, cfg, universe=None):
        super().__init__(cfg, universe)
        self.severity = self.rules.get("severity", "critical")

    def evaluate(self, rows: list[dict]) -> list[Signal]:
        signals: list[Signal] = []
        for row in rows:
            symbol = row.get("symbol") or ""
            if not symbol:
                continue
            extra = row.get("extra") or {}
            from_band = extra.get("from_band_pct")
            to_band = extra.get("to_band_pct")
            tightened = (from_band is not None and to_band is not None
                        and to_band < from_band)
            arrow = "▼" if tightened else "▲"
            direction = "tightened" if tightened else "widened"

            signals.append(Signal(
                category=self.category,
                rule_id=self.rule_id,
                entity=symbol,
                # New effective date -> new state, so a re-revision re-alerts.
                state_bucket=f"band_{extra.get('effective_date', '')}",
                severity=self.severity,
                title=(f"{arrow} PRICE BAND {direction} {symbol} "
                       f"{from_band:.0f}% -> {to_band:.0f}%"
                       if from_band is not None and to_band is not None
                       else f"{arrow} PRICE BAND {direction} {symbol}"),
                body=row.get("company") or "",
                value=to_band,
                payload={"symbol": symbol, "from_band_pct": from_band,
                         "to_band_pct": to_band,
                         "effective_date": extra.get("effective_date")},
            ))
        return signals
