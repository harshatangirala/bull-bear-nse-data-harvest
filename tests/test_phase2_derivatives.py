"""Phase 2, batch D: OI spurts, derivatives watch, most-active contracts,
option chain.

Payload fragments copied verbatim from live NSE responses captured
2026-08-29, except option_chain (see its own section below).
"""
from __future__ import annotations

from datetime import date

import pytest

from bbnse.fetchers.derivatives import (
    DerivativesWatchFetcher, MostActiveContractsFetcher,
)
from bbnse.fetchers.oi_spurts import OiSpurtsFetcher
from bbnse.fetchers.option_chain import OptionChainFetcher
from bbnse.processors.derivatives import (
    DerivativesWatchProcessor, MostActiveContractsProcessor,
    OiSpurtsProcessor, OptionChainProcessor,
)
from bbnse.processors.state import AlertStateMachine

from .helpers import FakeUniverse, make_cfg
from .test_phase2_equity import build

TODAY = date(2026, 8, 28)


# ==========================================================================
# oi_spurts
# ==========================================================================
OI_PAYLOAD = {"data": [{
    "symbol": "ATHERENERG", "latestOI": 15621, "prevOI": 5590,
    "changeInOI": 10031, "avgInOI": 179.45, "volume": 91084,
    "futValue": 51322.81567, "optValue": 51207140043,
    "total": 69528.14111, "premValue": 18205.32543,
    "underlyingValue": 1616,
}]}


def test_oi_change_in_oi_reconciles_with_latest_minus_prev():
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(OI_PAYLOAD)
    e = rows[0]["extra"]
    assert e["latest_oi"] - e["prev_oi"] == e["change_in_oi"] == 10031


def test_oi_avg_field_is_actually_a_percent_change():
    """NSE calls the field 'avgInOI'; it is (latest-prev)/prev*100.

    (15621-5590)/5590*100 = 179.45, matching the raw field exactly. Treating
    it as an average OI level -- the name NSE gave it -- would silently
    disable every threshold in this rule.
    """
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(OI_PAYLOAD)
    e = rows[0]["extra"]
    assert e["oi_change_pct"] == pytest.approx(179.45, rel=1e-4)
    recomputed = (15621 - 5590) / 5590 * 100
    assert e["oi_change_pct"] == pytest.approx(recomputed, rel=1e-4)
    assert "avg_oi" not in e         # must not be named as an average


def test_oi_change_pct_recomputed_when_field_absent():
    payload = {"data": [dict(OI_PAYLOAD["data"][0], avgInOI=None)]}
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(payload)
    assert rows[0]["extra"]["oi_change_pct"] == pytest.approx(179.45, rel=1e-3)


def test_oi_fut_and_prem_are_lakh_and_sum_to_total():
    """total == futValue + premValue on this feed, confirming a shared unit."""
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(OI_PAYLOAD)
    e = rows[0]["extra"]
    assert e["fut_value_cr"] + e["prem_value_cr"] == pytest.approx(
        rows[0]["traded_value"], rel=1e-6)
    assert e["fut_value_cr"] == pytest.approx(51322.81567 / 100)


def test_oi_opt_notional_is_a_different_unit_from_prem_value():
    """optValue is RUPEES notional -- orders of magnitude from premValue."""
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(OI_PAYLOAD)
    e = rows[0]["extra"]
    assert e["opt_notional_cr"] == pytest.approx(51207140043 / 1e7)
    # ~28x apart here; the live-sample median across 20 rows was ~5.8e6x
    # on the raw fields, confirming these are not the same quantity scaled.
    assert e["opt_notional_cr"] > e["prem_value_cr"] * 10


def test_oi_spurt_alerts_above_threshold():
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(OI_PAYLOAD)
    sigs = OiSpurtsProcessor(make_cfg(), FakeUniverse("ATHERENERG")).run(rows)
    assert len(sigs) == 1
    assert sigs[0].severity == "critical"      # 179% >= critical 40%
    assert "BUILD-UP" in sigs[0].title


def test_oi_unwind_is_a_separate_state():
    # latestOI kept above the 5000-contract min_oi_contracts floor so the
    # unwind is evaluated on its direction, not filtered as illiquid.
    payload = {"data": [dict(OI_PAYLOAD["data"][0], latestOI=8000, prevOI=15621,
                             changeInOI=-7621, avgInOI=-48.78)]}
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(payload)
    sigs = OiSpurtsProcessor(make_cfg(), FakeUniverse("ATHERENERG")).run(rows)
    assert sigs[0].state_bucket == "oi_unwind"
    assert "UNWIND" in sigs[0].title


def test_oi_spurt_respects_min_oi_floor():
    """A jump from 12 to 48 contracts is 300% but not tradeable size."""
    cfg = make_cfg(**{"rules.oi_spurts.min_oi_contracts": 1000})
    payload = {"data": [dict(OI_PAYLOAD["data"][0], latestOI=48, prevOI=12,
                             changeInOI=36, avgInOI=300.0)]}
    rows = build(OiSpurtsFetcher, cfg).normalize(payload)
    assert OiSpurtsProcessor(cfg, FakeUniverse("ATHERENERG")).run(rows) == []


def test_oi_spurt_debounces(dao):
    rows = build(OiSpurtsFetcher, make_cfg()).normalize(OI_PAYLOAD)
    proc = OiSpurtsProcessor(make_cfg(), FakeUniverse("ATHERENERG"))
    sm = AlertStateMachine(make_cfg(), dao)
    first = sm.process(proc.run(rows), category="oi_spurts", session_date=TODAY)
    second = sm.process(proc.run(rows), category="oi_spurts", session_date=TODAY)
    assert len(first) == 1 and second == []


# ==========================================================================
# derivatives_watch
# ==========================================================================
DERIV_PAYLOAD = {"data": [{
    "underlying": "NIFTY", "identifier": "FUTIDXNIFTY29-09-2026XX0.00",
    "instrumentType": "FUTIDX", "instrument": "Index Futures",
    "contract": "NIFTY 29-Sep-2026", "expiryDate": "29-Sep-2026",
    "optionType": "-", "strikePrice": 0, "lastPrice": 24349, "change": 67.1,
    "pChange": 0.28, "openPrice": 24292.7, "highPrice": 24380.2,
    "lowPrice": 24250, "closePrice": 24341.9, "volume": 2163135,
    "totalTurnover": 52594139919.75, "value": 52594139919.8,
}]}


def test_derivatives_watch_turnover_is_rupees():
    """vol*lastPrice/totalTurnover = 1.0014 across live rows -- RUPEES."""
    rows = build(DerivativesWatchFetcher, make_cfg()).normalize(DERIV_PAYLOAD)
    assert rows[0]["traded_value"] == pytest.approx(52594139919.75 / 1e7,
                                                     rel=1e-6)


def test_derivatives_watch_small_move_is_silent():
    rows = build(DerivativesWatchFetcher, make_cfg()).normalize(DERIV_PAYLOAD)
    assert DerivativesWatchProcessor(make_cfg()).run(rows) == []


def test_derivatives_watch_large_move_alerts():
    payload = {"data": [dict(DERIV_PAYLOAD["data"][0], pChange=7.5)]}
    rows = build(DerivativesWatchFetcher, make_cfg()).normalize(payload)
    sigs = DerivativesWatchProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1 and sigs[0].severity == "critical"


def test_derivatives_watch_entity_is_the_contract_not_the_underlying():
    payload = {"data": [dict(DERIV_PAYLOAD["data"][0], pChange=7.5)]}
    rows = build(DerivativesWatchFetcher, make_cfg()).normalize(payload)
    sigs = DerivativesWatchProcessor(make_cfg()).run(rows)
    assert sigs[0].entity == "NIFTY 29-SEP-2026"
    assert sigs[0].payload["underlying"] == "NIFTY"


# ==========================================================================
# most_active_contracts
# ==========================================================================
ACTIVE_CONTRACTS_PAYLOAD = {"volume": {"data": [
    {"identifier": "OPTIDXNIFTY01-09-2026PE24100.00", "instrumentType": "OPTIDX",
     "instrument": "Index Options", "underlying": "NIFTY",
     "expiryDate": "01-Sep-2026", "optionType": "Put", "strikePrice": 24100,
     "lastPrice": 35.25, "numberOfContractsTraded": 7597655,
     "totalTurnover": 282283.27387, "premiumTurnover": 119299548.84887,
     "openInterest": 152558, "underlyingValue": 24175.65,
     "pChange": -43.28238133547868},
]}}


def test_most_active_contracts_does_not_populate_unverified_turnover():
    """totalTurnover/premiumTurnover units could not be reconciled; the
    fetcher must not silently convert them into traded_value."""
    rows = build(MostActiveContractsFetcher, make_cfg()).normalize(
        ACTIVE_CONTRACTS_PAYLOAD)
    assert rows[0]["traded_value"] is None
    assert "raw_total_turnover_unverified" in rows[0]["extra"]


def test_most_active_contracts_ranks_by_position_in_the_list():
    payload = {"volume": {"data": ACTIVE_CONTRACTS_PAYLOAD["volume"]["data"] * 3}}
    rows = build(MostActiveContractsFetcher, make_cfg()).normalize(payload)
    assert [r["extra"]["rank"] for r in rows] == [1, 2, 3]


def test_most_active_contracts_alerts_on_pchange_only():
    rows = build(MostActiveContractsFetcher, make_cfg()).normalize(
        ACTIVE_CONTRACTS_PAYLOAD)
    sigs = MostActiveContractsProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1
    # |-43.28%| sits between notable(25) and critical(50): notable.
    assert sigs[0].severity == "notable"
    assert sigs[0].value == pytest.approx(-43.28238133547868)


def test_most_active_contracts_respects_top_n():
    payload = {"volume": {"data": ACTIVE_CONTRACTS_PAYLOAD["volume"]["data"] * 12}}
    cfg = make_cfg(**{"rules.most_active_contracts.top_n_by_turnover": 5})
    rows = build(MostActiveContractsFetcher, cfg).normalize(payload)
    sigs = MostActiveContractsProcessor(cfg).run(rows)
    assert len(sigs) <= 5


def test_most_active_contracts_dedupes_across_volume_and_value_lists():
    payload = {
        "volume": {"data": ACTIVE_CONTRACTS_PAYLOAD["volume"]["data"]},
        "value": {"data": ACTIVE_CONTRACTS_PAYLOAD["volume"]["data"]},
    }
    rows = build(MostActiveContractsFetcher, make_cfg()).normalize(payload)
    sigs = MostActiveContractsProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1     # same contract, appears in both rankings


# ==========================================================================
# option_chain (unverified units -- PCR-only rule)
# ==========================================================================
def _chain_row(strike, ce_oi, pe_oi, underlying=24175.65):
    return {
        "strikePrice": strike,
        "CE": {"openInterest": ce_oi, "totalTradedVolume": ce_oi // 10,
               "underlyingValue": underlying},
        "PE": {"openInterest": pe_oi, "totalTradedVolume": pe_oi // 10,
               "underlyingValue": underlying},
    }


CHAIN_PAYLOAD = {
    "records": {
        "timestamp": "28-Aug-2026 15:30:00", "underlying": "NIFTY",
        "data": [
            _chain_row(24000, 50000, 200000),
            _chain_row(24100, 80000, 150000),
            _chain_row(24200, 120000, 60000),
        ],
    }
}


def test_option_chain_empty_payload_yields_no_rows():
    """The endpoint returns {} outside market hours -- must not crash."""
    assert build(OptionChainFetcher, make_cfg()).normalize({}) == []
    assert build(OptionChainFetcher, make_cfg()).normalize(None) == []


def test_option_chain_pcr_is_computed_from_oi_sums():
    rows = build(OptionChainFetcher, make_cfg()).normalize(CHAIN_PAYLOAD)
    e = rows[0]["extra"]
    assert e["total_ce_oi"] == 250000
    assert e["total_pe_oi"] == 410000
    assert e["pcr"] == pytest.approx(410000 / 250000)


def test_option_chain_support_resistance_are_max_oi_strikes():
    rows = build(OptionChainFetcher, make_cfg()).normalize(CHAIN_PAYLOAD)
    e = rows[0]["extra"]
    assert e["max_ce_oi_strike"] == 24200      # highest CE OI -> resistance
    assert e["max_pe_oi_strike"] == 24000      # highest PE OI -> support


def test_option_chain_bullish_pcr_alerts():
    rows = build(OptionChainFetcher, make_cfg()).normalize(CHAIN_PAYLOAD)
    sigs = OptionChainProcessor(make_cfg()).run(rows)
    assert len(sigs) == 1 and "bullish" in sigs[0].title


def test_option_chain_neutral_pcr_is_silent():
    payload = {"records": {"underlying": "NIFTY", "data": [
        _chain_row(24000, 100000, 100000)]}}
    rows = build(OptionChainFetcher, make_cfg()).normalize(payload)
    assert OptionChainProcessor(make_cfg()).run(rows) == []


def test_option_chain_unit_is_explicitly_marked_unverified():
    rows = build(OptionChainFetcher, make_cfg()).normalize(CHAIN_PAYLOAD)
    assert rows[0]["extra"]["oi_unit"] == "UNVERIFIED"
