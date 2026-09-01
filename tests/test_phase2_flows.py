"""Phase 2, batch E: FII/DII flows and daily report availability.

Payload fragments copied verbatim from a live NSE response captured
2026-08-29.
"""
from __future__ import annotations

from datetime import date

import pytest

from bbnse.fetchers.flows import DailyReportsFetcher, FiiDiiFetcher
from bbnse.processors.flows import DailyReportsProcessor, FiiDiiProcessor
from bbnse.processors.state import AlertStateMachine

from .helpers import make_cfg
from .test_phase2_equity import build

TODAY = date(2026, 8, 28)

FII_DII_PAYLOAD = [
    {"buyValue": "16539.38", "category": "DII", "date": "28-Aug-2026",
     "netValue": "5183.93", "sellValue": "11355.45"},
    {"buyValue": "13263.62", "category": "FII/FPI", "date": "28-Aug-2026",
     "netValue": "-5039.8", "sellValue": "18303.42"},
]


def test_fii_dii_payload_is_a_bare_list():
    """Unlike every other endpoint, this one is not wrapped in {'data': ...}."""
    rows = build(FiiDiiFetcher, make_cfg()).normalize(FII_DII_PAYLOAD)
    assert len(rows) == 2


def test_fii_dii_values_are_already_crore():
    """DII buy 16539.38 - sell 11355.45 = net 5183.93, exactly as reported.

    Figures of this magnitude for a single session can only be crore -- as
    rupees or lakh they would be nonsensical. This is the one feed already in
    the target unit, so cr_to_cr() is used to make that explicit rather than
    silently passing the string through.
    """
    rows = build(FiiDiiFetcher, make_cfg()).normalize(FII_DII_PAYLOAD)
    dii = next(r for r in rows if r["symbol"] == "DII")
    assert dii["extra"]["buy_cr"] == pytest.approx(16539.38)
    assert dii["extra"]["net_cr"] == pytest.approx(5183.93)
    assert dii["extra"]["buy_cr"] - dii["extra"]["sell_cr"] == pytest.approx(
        dii["extra"]["net_cr"], abs=0.01)


def test_fii_dii_net_value_parses_negative_strings():
    rows = build(FiiDiiFetcher, make_cfg()).normalize(FII_DII_PAYLOAD)
    fii = next(r for r in rows if r["symbol"] == "FII/FPI")
    assert fii["extra"]["net_cr"] == pytest.approx(-5039.8)


def test_fii_dii_net_selling_alerts():
    rows = build(FiiDiiFetcher, make_cfg()).normalize(FII_DII_PAYLOAD)
    sigs = FiiDiiProcessor(make_cfg()).run(rows)
    fii_sig = next(s for s in sigs if "FII" in s.entity)
    assert fii_sig.severity == "critical"      # |-5039.8| >= critical 5000
    assert "selling" in fii_sig.title


def test_fii_dii_net_buying_is_notable():
    rows = build(FiiDiiFetcher, make_cfg()).normalize(FII_DII_PAYLOAD)
    sigs = FiiDiiProcessor(make_cfg()).run(rows)
    dii_sig = next(s for s in sigs if s.entity == "DII")
    assert dii_sig.severity == "critical"      # 5183.93 >= critical 5000
    assert "buying" in dii_sig.title


def test_fii_dii_below_threshold_is_silent():
    cfg = make_cfg(**{"rules.fii_dii.net_cr_notable": 99999.0})
    rows = build(FiiDiiFetcher, cfg).normalize(FII_DII_PAYLOAD)
    assert FiiDiiProcessor(cfg).run(rows) == []


def test_fii_dii_state_keyed_on_date_survives_catchup_replay(dao):
    """A catch-up backfill re-running the same day's job must not re-alert."""
    rows = build(FiiDiiFetcher, make_cfg()).normalize(FII_DII_PAYLOAD)
    proc = FiiDiiProcessor(make_cfg())
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="fii_dii", session_date=TODAY)
    second = sm.process(proc.run(rows), category="fii_dii", session_date=TODAY)
    assert len(first) == 2 and second == []


# ==========================================================================
# daily_reports
# ==========================================================================
def test_daily_reports_empty_response_is_tolerated():
    """{'data': [], 'msg': 'no data found'} outside publishing hours."""
    payload = {"data": [], "msg": "no data found"}
    assert build(DailyReportsFetcher, make_cfg()).normalize(payload) == []


def test_daily_reports_extracts_name_and_link():
    payload = {"data": [{"name": "Bhavcopy (Equity)",
                         "link": "/reports/bhavcopy.csv"}]}
    rows = build(DailyReportsFetcher, make_cfg()).normalize(payload)
    assert rows[0]["extra"]["report_name"] == "Bhavcopy (Equity)"
    assert rows[0]["extra"]["link"] == "/reports/bhavcopy.csv"


def test_daily_reports_alerts_once_per_report(dao):
    payload = {"data": [{"name": "Bhavcopy (Equity)", "link": "x"}]}
    rows = build(DailyReportsFetcher, make_cfg()).normalize(payload)
    proc = DailyReportsProcessor(make_cfg())
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="daily_reports",
                       session_date=TODAY)
    second = sm.process(proc.run(rows), category="daily_reports",
                        session_date=TODAY)
    assert len(first) == 1 and second == []


def test_daily_reports_watch_filter():
    cfg = make_cfg(**{"rules.daily_reports.watch_reports": ["bhavcopy"]})
    payload = {"data": [{"name": "Bhavcopy (Equity)", "link": "x"},
                        {"name": "Circulars Digest", "link": "y"}]}
    rows = build(DailyReportsFetcher, cfg).normalize(payload)
    sigs = DailyReportsProcessor(cfg).run(rows)
    assert len(sigs) == 1
    assert "Bhavcopy" in sigs[0].title
