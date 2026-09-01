"""Phase 3: the 3 originally-unresolved categories (GSM, ASM, surveillance
price bands, SLB, closing auction session).

Discovery note: none of these turned up via the landing pages' script tags
on the first pass -- the page-specific bundle for each was hiding behind a
`?v=` cache-busting query string that an earlier ".js"-suffix-only regex
missed. Payload fragments below are copied verbatim from live NSE responses
captured 2026-09-01.
"""
from __future__ import annotations

from datetime import date

import pytest

from bbnse.fetchers.closing_auction import ClosingAuctionFetcher
from bbnse.fetchers.slb import SlbFetcher
from bbnse.fetchers.surveillance import (
    AsmFetcher, GsmFetcher, SurveillancePriceBandsFetcher,
)
from bbnse.processors.closing_auction import ClosingAuctionProcessor
from bbnse.processors.slb import SlbProcessor
from bbnse.processors.state import AlertStateMachine
from bbnse.processors.surveillance import (
    AsmProcessor, GsmProcessor, SurveillancePriceBandsProcessor,
)

from .helpers import FakeUniverse, make_cfg
from .test_phase2_equity import build

TODAY = date(2026, 9, 1)


# ==========================================================================
# GSM
# ==========================================================================
GSM_PAYLOAD = [
    {"companyName": "AGS Transact Technologies Limited", "gsmStage": "LXII",
     "gsmTime": "01-Sep-2026 08:07:02", "isin": "INE583L01014",
     "survCode": "IBC - Receipt & GSM 0 (62)",
     "survDesc": "Insolvency and Bankruptcy Code (IBC) - Receipt of "
                 "Disclosure or Recommenced scrip and GSM stage 0",
     "symbol": "AGSTRA", "srno": 1},
]


def test_gsm_payload_is_a_bare_list():
    rows = build(GsmFetcher, make_cfg()).normalize(GSM_PAYLOAD)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AGSTRA"
    assert rows[0]["extra"]["gsm_stage"] == "LXII"


def test_gsm_parses_its_own_timestamp():
    fetcher = build(GsmFetcher, make_cfg())
    assert fetcher.payload_trade_date(GSM_PAYLOAD) == date(2026, 9, 1)


def test_gsm_tolerates_empty_or_non_list_payload():
    assert build(GsmFetcher, make_cfg()).normalize(None) == []
    assert build(GsmFetcher, make_cfg()).normalize({}) == []
    assert build(GsmFetcher, make_cfg()).normalize([]) == []


def test_gsm_alerts_once_per_stage(dao):
    rows = build(GsmFetcher, make_cfg()).normalize(GSM_PAYLOAD)
    proc = GsmProcessor(make_cfg())
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="gsm", session_date=TODAY)
    second = sm.process(proc.run(rows), category="gsm", session_date=TODAY)
    assert len(first) == 1 and second == []


def test_gsm_stage_change_reopens_the_alert(dao):
    """A stock moving from one GSM stage to another must alert again --
    the state_bucket is keyed on the stage itself."""
    rows = build(GsmFetcher, make_cfg()).normalize(GSM_PAYLOAD)
    proc = GsmProcessor(make_cfg())
    sm = AlertStateMachine(make_cfg(), dao)
    sm.process(proc.run(rows), category="gsm", session_date=TODAY)

    escalated = [dict(GSM_PAYLOAD[0], gsmStage="LXIII")]
    rows2 = build(GsmFetcher, make_cfg()).normalize(escalated)
    second = sm.process(proc.run(rows2), category="gsm", session_date=TODAY)
    assert len(second) == 1


# ==========================================================================
# ASM
# ==========================================================================
ASM_PAYLOAD = {
    "longterm": {"data": [
        {"asmSurvIndicator": "Stage I", "asmTime": "01-Sep-2026",
         "companyName": "A2Z Infra Engineering Limited",
         "isin": "INE619I01012", "series": None,
         "survCode": "LTASM - I (13)",
         "survDesc": "Long Term Additional Surveillance Measure (LTASM) - "
                     "Stage I",
         "symbol": "A2ZINFRA", "srno": 1},
    ]},
    "shortterm": {"data": [
        {"asmSurvIndicator": "Stage I", "asmTime": "01-Sep-2026",
         "companyName": "Aastha Spintex Limited", "isin": "INE2FMX01012",
         "series": None, "survCode": "STASM - I (11)",
         "survDesc": "Short Term Additional Surveillance Measure (STASM) - "
                     "Stage I",
         "symbol": "AASTHA", "srno": 1},
    ]},
}


def test_asm_covers_both_longterm_and_shortterm_buckets():
    rows = build(AsmFetcher, make_cfg()).normalize(ASM_PAYLOAD)
    assert {r["bucket"] for r in rows} == {"longterm", "shortterm"}
    assert len(rows) == 2


def test_asm_parses_timestamp_from_either_bucket():
    fetcher = build(AsmFetcher, make_cfg())
    assert fetcher.payload_trade_date(ASM_PAYLOAD) == date(2026, 9, 1)


def test_asm_tolerates_one_empty_bucket():
    payload = {"longterm": {"data": ASM_PAYLOAD["longterm"]["data"]},
              "shortterm": {"data": []}}
    rows = build(AsmFetcher, make_cfg()).normalize(payload)
    assert len(rows) == 1


def test_asm_alerts_for_both_terms_by_default():
    rows = build(AsmFetcher, make_cfg()).normalize(ASM_PAYLOAD)
    sigs = AsmProcessor(make_cfg()).run(rows)
    assert len(sigs) == 2


def test_asm_term_filter():
    cfg = make_cfg(**{"rules.asm.terms": ["shortterm"]})
    rows = build(AsmFetcher, cfg).normalize(ASM_PAYLOAD)
    sigs = AsmProcessor(cfg).run(rows)
    assert len(sigs) == 1
    assert sigs[0].entity == "AASTHA"


def test_asm_alerts_once_per_stage(dao):
    rows = build(AsmFetcher, make_cfg()).normalize(ASM_PAYLOAD)
    proc = AsmProcessor(make_cfg())
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="asm", session_date=TODAY)
    second = sm.process(proc.run(rows), category="asm", session_date=TODAY)
    assert len(first) == 2 and second == []


# ==========================================================================
# surveillance price bands
# ==========================================================================
BAND_CHANGE_PAYLOAD = [
    {"effectiveDate": "01-SEP-2026", "fromPriceBand": "10",
     "secName": "BURNPUR CEMENT LIMITED", "symbol": "BURNPUR",
     "toPriceBand": "5", "srno": 1},
    {"effectiveDate": "01-SEP-2026", "fromPriceBand": "5",
     "secName": "SOME WIDENED LIMITED", "symbol": "WIDECO",
     "toPriceBand": "10", "srno": 2},
]


def test_surveillance_price_bands_no_unit_conversion_needed():
    """fromPriceBand/toPriceBand are percent band widths, not prices."""
    rows = build(SurveillancePriceBandsFetcher, make_cfg()).normalize(
        BAND_CHANGE_PAYLOAD)
    burnpur = next(r for r in rows if r["symbol"] == "BURNPUR")
    assert burnpur["extra"]["from_band_pct"] == 10.0
    assert burnpur["extra"]["to_band_pct"] == 5.0


def test_surveillance_price_bands_detects_tightening_vs_widening():
    rows = build(SurveillancePriceBandsFetcher, make_cfg()).normalize(
        BAND_CHANGE_PAYLOAD)
    sigs = SurveillancePriceBandsProcessor(make_cfg()).run(rows)
    burnpur = next(s for s in sigs if s.entity == "BURNPUR")
    wideco = next(s for s in sigs if s.entity == "WIDECO")
    assert "tightened" in burnpur.title
    assert "widened" in wideco.title


def test_surveillance_price_bands_default_severity_is_critical():
    rows = build(SurveillancePriceBandsFetcher, make_cfg()).normalize(
        BAND_CHANGE_PAYLOAD)
    sigs = SurveillancePriceBandsProcessor(make_cfg()).run(rows)
    assert all(s.severity == "critical" for s in sigs)


def test_surveillance_price_bands_ignores_universe():
    """A stock getting its band cut is worth knowing regardless of index
    membership -- same reasoning as large_deals."""
    rows = build(SurveillancePriceBandsFetcher, make_cfg()).normalize(
        BAND_CHANGE_PAYLOAD)
    sigs = SurveillancePriceBandsProcessor(
        make_cfg(), FakeUniverse("SOMETHINGELSE")).run(rows)
    assert len(sigs) == 2


def test_surveillance_price_bands_keyed_on_effective_date(dao):
    """A second revision on a later date must re-alert, not be suppressed."""
    rows = build(SurveillancePriceBandsFetcher, make_cfg()).normalize(
        BAND_CHANGE_PAYLOAD)
    proc = SurveillancePriceBandsProcessor(make_cfg())
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="surveillance_price_bands",
                       session_date=TODAY)
    second = sm.process(proc.run(rows), category="surveillance_price_bands",
                        session_date=TODAY)
    assert len(first) == 2 and second == []

    later = [dict(BAND_CHANGE_PAYLOAD[0], effectiveDate="15-SEP-2026",
                  toPriceBand="2")]
    rows2 = build(SurveillancePriceBandsFetcher, make_cfg()).normalize(later)
    third = sm.process(proc.run(rows2), category="surveillance_price_bands",
                       session_date=date(2026, 9, 15))
    assert len(third) == 1


# ==========================================================================
# SLB
# ==========================================================================
SLB_PAYLOAD = {"data": [
    {"symbol": "IREDA", "buyOrderPrice1": 0, "buyOrderQty1": 0,
     "sellOrderPrice1": 0, "sellQty1": 0, "lastTradedPrice": 0,
     "underLyingLtp": 115.05, "futuresLtp": 111.2, "spread": 3.85,
     "spreadPer": 3.35, "openPositions": 0, "annualisedYieldPer": 0,
     "volume": 0, "turnOver": 0, "transactionValue": 0,
     "caExpDate": "02-Apr-2026",
     "ca": "INTERIM DIVIDEND - RE 0.60 PER SHARE 02-Apr-2026",
     "meta": {"isSLBSec": True}},
], "timestamp": "01-Sep-2026 11:40:00", "meta": {}, "marketStatus": {}}

SLB_SERIES_MASTER = {
    "data": {"Series A": [{"key": "10", "value": "Oct-2026"}]},
    "filter": {"series": {"key": "10", "value": "Oct-2026"}},
}


def test_slb_spread_pct_is_dimensionless_percent():
    """spreadPer == spread/underLyingLtp*100: 3.85/115.05*100 = 3.347,
    matching the reported 3.35 to the payload's own rounding."""
    rows = build(SlbFetcher, make_cfg()).normalize(SLB_PAYLOAD)
    row = rows[0]
    recomputed = row["extra"]["spread"] / row["last_price"] * 100
    assert row["extra"]["spread_pct"] == pytest.approx(recomputed, abs=0.01)


def test_slb_unverified_fields_are_labelled_not_converted():
    rows = build(SlbFetcher, make_cfg()).normalize(SLB_PAYLOAD)
    assert "turnover_unverified" in rows[0]["extra"]
    assert "transaction_value_unverified" in rows[0]["extra"]


def test_slb_fetcher_refreshes_series_key_before_polling():
    """The series is not hardcoded -- it comes from series-master's
    filter.series.key each poll, since it rolls over monthly."""
    class _StubSession:
        def __init__(self):
            self.calls = []

        def warm(self, referer):
            pass

        def get_json(self, url, referer=None, params=None, timeout=None):
            self.calls.append((url, params))
            class R:
                ok = True
                status = 200
                content = b"{}"
                text = "{}"
            r = R()
            if "series-master" in url:
                r.json = SLB_SERIES_MASTER
            else:
                r.json = SLB_PAYLOAD
            return r

    class _StubDao:
        def store_snapshot(self, *a, **k):
            return (1, True)

        def add_observations(self, rows):
            return len(rows)

    session = _StubSession()
    fetcher = SlbFetcher(make_cfg(), session, _StubDao())
    outcome = fetcher.run()

    assert outcome.ok
    data_calls = [c for c in session.calls if "series-master" not in c[0]]
    assert data_calls[0][1] == {"series": "10"}


def test_slb_respects_universe():
    rows = build(SlbFetcher, make_cfg()).normalize(SLB_PAYLOAD)
    assert SlbProcessor(make_cfg(), FakeUniverse("SOMETHINGELSE")).run(rows) == []


def test_slb_wide_spread_alerts():
    rows = build(SlbFetcher, make_cfg()).normalize(SLB_PAYLOAD)
    sigs = SlbProcessor(make_cfg(), FakeUniverse("IREDA")).run(rows)
    assert len(sigs) == 1
    assert sigs[0].severity == "notable"      # 3.35% < critical 6.0


def test_slb_narrow_spread_and_low_yield_is_silent():
    payload = {"data": [dict(SLB_PAYLOAD["data"][0], spreadPer=0.5,
                             annualisedYieldPer=1.0)]}
    rows = build(SlbFetcher, make_cfg()).normalize(payload)
    assert SlbProcessor(make_cfg(), FakeUniverse("IREDA")).run(rows) == []


# ==========================================================================
# Closing Auction Session
# ==========================================================================
def test_cas_tolerates_empty_data():
    """CAS is empty outside its narrow pre-close window -- must not crash."""
    payload = {"data": [], "totalValue": 0, "totalQuantity": 0}
    assert build(ClosingAuctionFetcher, make_cfg()).normalize(payload) == []
    assert build(ClosingAuctionFetcher, make_cfg()).normalize(None) == []


def test_cas_traded_value_conversion():
    """finalValue is rupees per the frontend's own Lakhs/Crores/Billions
    divisor logic (see fetchers/closing_auction.py docstring)."""
    payload = {"data": [{
        "symbol": "TESTCO", "refrencePrice": 100.0, "lowerBand": 80.0,
        "upperBand": 120.0, "bestBidQty": 100, "bestBidPrice": 99.0,
        "bestAskPrice": 101.0, "bestAskQty": 100, "totTradedQty": 5000,
        "IEP": 110.0, "change": 10.0, "perChange": 10.0,
        "finalPrice": 110.0, "finalQuantity": 5000, "finalValue": 550000000,
        "iiqAtEP": 0, "iiqAtMO": 0,
    }], "timestamp": "01-Sep-2026 15:29:00"}
    rows = build(ClosingAuctionFetcher, make_cfg()).normalize(payload)
    assert rows[0]["traded_value"] == pytest.approx(550000000 / 1e7)


def test_cas_large_move_alerts():
    payload = {"data": [{
        "symbol": "TESTCO", "refrencePrice": 100.0, "finalPrice": 115.0,
        "perChange": 15.0, "finalQuantity": 1000, "finalValue": 115000,
        "IEP": 115.0,
    }]}
    rows = build(ClosingAuctionFetcher, make_cfg()).normalize(payload)
    sigs = ClosingAuctionProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1
    assert sigs[0].severity == "critical"      # 15% >= critical 10.0


def test_cas_small_move_is_silent():
    payload = {"data": [{
        "symbol": "TESTCO", "refrencePrice": 100.0, "finalPrice": 101.0,
        "perChange": 1.0, "finalQuantity": 1000, "finalValue": 101000,
    }]}
    rows = build(ClosingAuctionFetcher, make_cfg()).normalize(payload)
    assert ClosingAuctionProcessor(make_cfg()).run(rows) == []
