"""Phase 2, batch C: pre-open session.

Payload fragments copied verbatim from a live NSE response captured
2026-08-29 (2072 rows in the real payload; trimmed here to what each test
needs).
"""
from __future__ import annotations

from datetime import date

import pytest

from bbnse.fetchers.pre_open import PreOpenCashFetcher, PreOpenFnoFetcher
from bbnse.processors.pre_open import PreOpenProcessor
from bbnse.processors.state import AlertStateMachine

from .helpers import FakeUniverse, make_cfg
from .test_phase2_equity import build

TODAY = date(2026, 8, 28)

PRE_OPEN_PAYLOAD = {
    "timestamp": "28-Aug-2026 09:07:00",
    "advances": 1244, "declines": 542, "unchanged": 286,
    "totalTradedValue": 3760423523, "totalTradedVolume": 16758723,
    "data": [
        {
            "metadata": {
                "symbol": "LANCORHOL", "series": "EQ", "lastPrice": 33.00,
                "change": 2.5, "pChange": 8.19, "previousClose": 30.5,
                "finalQuantity": 202763, "totalTurnover": 6691179,
                "marketCap": "-", "yearHigh": 40.0, "yearLow": 12.0,
                "iep": 33.00,
            },
            "detail": {"preOpenMarket": {"preopen": []}},
        },
        {
            "metadata": {
                "symbol": "TECILCHEM", "series": "EQ", "lastPrice": 9.98,
                "change": 0.89, "pChange": 9.79, "previousClose": 9.09,
                "finalQuantity": 20, "totalTurnover": 199,
                "marketCap": "-", "yearHigh": 22.87, "yearLow": 8,
                "iep": 9.98,
            },
            "detail": {},
        },
        {
            # Real turnover behind the move -- unlike TECILCHEM's Rs 199,
            # which exists specifically to exercise the min_value_cr floor.
            "metadata": {
                "symbol": "BURNPUR", "series": "EQ", "lastPrice": 16.15,
                "change": 1.8, "pChange": 12.55, "previousClose": 14.35,
                "finalQuantity": 1500000, "totalTurnover": 24225000,
                "marketCap": "-", "yearHigh": 20.0, "yearLow": 9.0,
                "iep": 16.15,
            },
            "detail": {},
        },
        {
            # No metadata -- must be skipped, not crash.
            "detail": {},
        },
    ],
}


def test_pre_open_turnover_is_rupees():
    """202,763 x 33.00 = 6,691,179 rupees = 0.669 crore. Verified exactly."""
    rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    lancor = next(r for r in rows if r["symbol"] == "LANCORHOL")
    assert lancor["traded_value"] == pytest.approx(6691179 / 1e7)


def test_pre_open_skips_rows_without_metadata():
    rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    assert len(rows) == 3


def test_pre_open_market_totals_are_extracted():
    totals = build(PreOpenCashFetcher, make_cfg()).market_totals(PRE_OPEN_PAYLOAD)
    assert totals["advances"] == 1244
    assert totals["declines"] == 542
    assert totals["total_traded_value_cr"] == pytest.approx(3760423523 / 1e7)


def test_pre_open_tolerates_non_dict_payload():
    assert build(PreOpenCashFetcher, make_cfg()).normalize(None) == []
    assert build(PreOpenCashFetcher, make_cfg()).normalize([]) == []


def test_pre_open_cash_and_fno_are_distinct_categories():
    cm_rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    fo_rows = build(PreOpenFnoFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    assert cm_rows[0]["category"] == "pre_open_cm"
    assert fo_rows[0]["category"] == "pre_open_fo"
    assert cm_rows[0]["extra"]["segment"] == "CM"
    assert fo_rows[0]["extra"]["segment"] == "FO"


def test_pre_open_gap_alerts_above_threshold():
    """BURNPUR has both the % move and real turnover behind it."""
    rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    sigs = PreOpenProcessor(make_cfg(), FakeUniverse("BURNPUR"),
                            category="pre_open_cm").run(rows)
    assert len(sigs) == 1
    assert "BURNPUR" in sigs[0].title
    assert "gap up" in sigs[0].title


def test_pre_open_respects_min_value_floor():
    """TECILCHEM traded only Rs 199 -- filtered out at the default floor
    even though its 9.79% move clears the notable threshold."""
    rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    sigs = PreOpenProcessor(make_cfg(), FakeUniverse("TECILCHEM"),
                            category="pre_open_cm").run(rows)
    assert sigs == []


def test_pre_open_respects_universe():
    rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    sigs = PreOpenProcessor(make_cfg(), FakeUniverse("SOMETHINGELSE"),
                            category="pre_open_cm").run(rows)
    assert sigs == []


def test_pre_open_low_value_row_survives_below_normalize_but_filtered_by_rule():
    """The unit conversion itself must be correct even when the rule filters
    the row out -- so the tiny Rs 199 turnover is checked precisely."""
    rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    tecil = next(r for r in rows if r["symbol"] == "TECILCHEM")
    assert tecil["traded_value"] == pytest.approx(199 / 1e7)


def test_pre_open_alerts_once(dao):
    rows = build(PreOpenCashFetcher, make_cfg()).normalize(PRE_OPEN_PAYLOAD)
    proc = PreOpenProcessor(make_cfg(), FakeUniverse("BURNPUR"),
                            category="pre_open_cm")
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="pre_open_cm",
                       session_date=TODAY)
    second = sm.process(proc.run(rows), category="pre_open_cm",
                        session_date=TODAY)
    assert len(first) == 1 and second == []


def test_pre_open_persist_filter_drops_small_moves():
    """The 2072-row payload must not become 2072 stored observations."""
    fetcher = build(PreOpenCashFetcher, make_cfg())
    rows = fetcher.normalize(PRE_OPEN_PAYLOAD)
    # LANCORHOL +8.19%, TECILCHEM +9.79% -- both clear the 2.0% floor here,
    # so add a sub-threshold row to prove the filter actually filters.
    quiet = dict(rows[0])
    quiet["symbol"] = "QUIETCO"
    quiet["pct_change"] = 0.3
    captured = []

    class _Dao:
        def add_observations(self, r):
            captured.extend(r)
            return len(r)

    fetcher.dao = _Dao()
    fetcher.persist(rows + [quiet], TODAY)
    assert "QUIETCO" not in {r["symbol"] for r in captured}
    assert {"LANCORHOL", "TECILCHEM"} <= {r["symbol"] for r in captured}
