"""Offline tests for the logic that is easy to get subtly wrong.

No network. Everything here uses an in-memory SQLite DB and hand-built
payloads shaped like the real NSE responses (field misspellings included).

    python -m pytest tests/ -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from bbnse.fetchers.base import lakh_to_cr, parse_nse_date, to_float, to_int
from bbnse.processors.base import Signal
from bbnse.processors.large_deals import LargeDealsProcessor
from bbnse.processors.state import AlertStateMachine, event_key
from bbnse.processors.week52 import Week52Processor
from bbnse.storage.dao import Dao, content_hash

from .helpers import FakeUniverse, make_cfg


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------
def test_to_float_handles_nse_string_variants():
    assert to_float("1,234.50") == 1234.50     # comma-formatted string
    assert to_float(42) == 42.0
    assert to_float("-") is None               # NSE's empty marker
    assert to_float(None) is None
    assert to_float("NA") is None
    assert to_int("906,031") == 906031


def test_turnover_is_converted_from_lakh_to_crore():
    # NSE reports turnover in lakh; every threshold in config is in crore.
    assert lakh_to_cr(94377.51) == pytest.approx(943.7751)


def test_parse_nse_date_formats():
    assert parse_nse_date("28-Aug-2026") == date(2026, 8, 28)
    assert parse_nse_date("28-Aug-2026 15:30:00") == date(2026, 8, 28)
    assert parse_nse_date("") is None
    assert parse_nse_date("garbage") is None


# --------------------------------------------------------------------------
# snapshot dedup
# --------------------------------------------------------------------------
def test_content_hash_ignores_row_order():
    """NSE returns the same rows in a different order between polls."""
    a = {"allSec": {"data": [{"symbol": "A", "ltp": 1}, {"symbol": "B", "ltp": 2}]}}
    b = {"allSec": {"data": [{"symbol": "B", "ltp": 2}, {"symbol": "A", "ltp": 1}]}}
    assert content_hash(a) == content_hash(b)


def test_content_hash_still_detects_real_change():
    a = {"data": [{"symbol": "A", "ltp": 1}]}
    b = {"data": [{"symbol": "A", "ltp": 2}]}
    assert content_hash(a) != content_hash(b)


def test_store_snapshot_dedupes(dao: Dao):
    payload = {"data": [{"symbol": "A"}]}
    id1, new1 = dao.store_snapshot("gainers", payload)
    id2, new2 = dao.store_snapshot("gainers", payload)
    assert new1 is True and new2 is False and id1 == id2


# --------------------------------------------------------------------------
# 52-week processor
# --------------------------------------------------------------------------
def _w52_row(**kw):
    row = {"symbol": "ACME", "company": "Acme Ltd", "last_price": 100.0,
           "extreme_value": 110.0, "prev_extreme": 105.0,
           "prev_extreme_date": "10-Jun-2026", "pct_change": 2.0,
           "extra": {"margin_pct": 4.76, "kind": "high"}}
    row.update(kw)
    return row


def test_week52_high_escalates_on_wide_breakout():
    proc = Week52Processor(make_cfg(), None, category="week52_high")
    sigs = proc.run([_w52_row()])
    assert len(sigs) == 1
    assert sigs[0].severity == "critical"      # margin 4.76 >= 3.0
    assert sigs[0].value == 110.0


def test_week52_narrow_breakout_is_only_notable():
    proc = Week52Processor(make_cfg(), None, category="week52_high")
    sigs = proc.run([_w52_row(extra={"margin_pct": 0.4, "kind": "high"})])
    assert sigs[0].severity == "notable"


def test_week52_skips_penny_stocks():
    proc = Week52Processor(make_cfg(), None, category="week52_high")
    assert proc.run([_w52_row(last_price=5.0)]) == []


def test_week52_low_margin_sign_is_normalised():
    """For lows, a *lower* new extreme must count as a wider breakout.

    The fetcher negates the raw percentage for lows so that positive always
    means 'more extreme'; the processor must not re-invert it.
    """
    proc = Week52Processor(make_cfg(), None, category="week52_low")
    sigs = proc.run([_w52_row(extreme_value=90.0, prev_extreme=95.0,
                              extra={"margin_pct": 5.26, "kind": "low"})])
    assert len(sigs) == 1 and sigs[0].severity == "critical"


def test_week52_respects_universe():
    proc = Week52Processor(make_cfg(), FakeUniverse("INUNIVERSE"),
                           category="week52_high")
    assert proc.run([_w52_row(symbol="NOTLISTED")]) == []
    assert len(proc.run([_w52_row(symbol="INUNIVERSE")])) == 1


# --------------------------------------------------------------------------
# large deals processor
# --------------------------------------------------------------------------
def _deal(**kw):
    row = {"deal_type": "BULK", "symbol": "ACME", "company": "Acme Ltd",
           "client_name": "SOME FUND", "buy_sell": "BUY", "quantity": 100000,
           "price": 1500.0, "value_cr": 15.0, "remarks": "-",
           "trade_date": date(2026, 8, 28), "dedupe_key": "k1"}
    row.update(kw)
    return row


def test_large_deal_below_threshold_is_ignored():
    proc = LargeDealsProcessor(make_cfg(), None)
    assert proc.run([_deal(value_cr=3.0)]) == []


def test_large_deal_critical_above_threshold():
    proc = LargeDealsProcessor(make_cfg(), None)
    sigs = proc.run([_deal(value_cr=80.0)])
    assert len(sigs) == 1 and sigs[0].severity == "critical"


def test_block_deals_use_their_own_higher_floor():
    """A 15 cr BLOCK is below the 25 cr block floor, but a 15 cr BULK is not."""
    proc = LargeDealsProcessor(make_cfg(), None)
    assert proc.run([_deal(deal_type="BLOCK", value_cr=15.0)]) == []
    assert len(proc.run([_deal(deal_type="BULK", value_cr=15.0)])) == 1


def test_same_trade_in_both_feeds_gets_one_dedup_key():
    """A block large enough to also qualify as bulk appears in both feeds.

    Collapsing them is the deduplicator's job (see test_correlate.py). The
    processor's contract is narrower and is what is asserted here: emit both
    rows, give them an identical dedup_key because they are one trade, and
    rank the block feed higher so it becomes the headline.
    """
    common = dict(symbol="ATHERENERG", client_name="HERO MOTOCORP LIMITED",
                  buy_sell="BUY", quantity=11880000, price=1480.0,
                  value_cr=1758.2, trade_date=date(2026, 8, 28))
    rows = [_deal(deal_type="BULK", dedupe_key="b1", **common),
            _deal(deal_type="BLOCK", dedupe_key="b2", **common)]
    sigs = LargeDealsProcessor(make_cfg(), None).run(rows)

    assert len(sigs) == 2
    assert sigs[0].dedup_key == sigs[1].dedup_key          # one trade
    by_label = {s.dedup_label: s for s in sigs}
    assert by_label["block feed"].dedup_priority > by_label["bulk feed"].dedup_priority


def test_distinct_trades_get_distinct_dedup_keys():
    """Different client or size must not be merged as one trade."""
    proc = LargeDealsProcessor(make_cfg(), None)
    a = proc.run([_deal(value_cr=60.0, client_name="FUND A")])[0]
    b = proc.run([_deal(value_cr=60.0, client_name="FUND B")])[0]
    c = proc.run([_deal(value_cr=60.0, quantity=99999)])[0]
    assert len({a.dedup_key, b.dedup_key, c.dedup_key}) == 3


def test_end_to_end_bulk_block_collapse(dao):
    """The processor and deduplicator together reproduce the Ather case."""
    from bbnse.processors.correlate import CrossFeedDeduplicator

    common = dict(symbol="ATHERENERG", client_name="HERO MOTOCORP LIMITED",
                  buy_sell="BUY", quantity=11880000, price=1480.0,
                  value_cr=1758.2, trade_date=date(2026, 8, 28))
    rows = [_deal(deal_type="BULK", dedupe_key="b1", **common),
            _deal(deal_type="BLOCK", dedupe_key="b2", **common)]
    cfg = make_cfg()
    sigs = LargeDealsProcessor(cfg, None).run(rows)
    out = CrossFeedDeduplicator(cfg, dao).apply(sigs,
                                                session_date=date(2026, 8, 28))
    assert len(out) == 1
    assert "BLOCK" in out[0].title
    assert "also in: bulk feed" in out[0].body


def test_watch_client_alerts_regardless_of_size():
    cfg = make_cfg(**{"rules.large_deals": {
        "enabled": True, "value_cr_notable": 10.0, "value_cr_critical": 50.0,
        "block_value_cr_notable": 25.0, "deal_types": ["BULK"],
        "watch_clients": ["GRAVITON"], "respect_universe": False}})
    proc = LargeDealsProcessor(cfg, None)
    sigs = proc.run([_deal(value_cr=0.5,
                           client_name="GRAVITON RESEARCH CAPITAL LLP")])
    assert len(sigs) == 1 and "watched client" in sigs[0].body


# --------------------------------------------------------------------------
# alert state machine -- the debounce contract
# --------------------------------------------------------------------------
def _sig(entity="ACME", value=100.0, bucket="week52_high"):
    return Signal(category="week52_high", rule_id="fresh_extreme",
                  entity=entity, state_bucket=bucket, severity="notable",
                  title=f"{entity} high", value=value)


def test_alert_fires_once_then_stays_silent(dao: Dao):
    sm = AlertStateMachine(make_cfg(), dao)
    today = date(2026, 8, 28)
    first = sm.process([_sig()], category="week52_high", session_date=today)
    second = sm.process([_sig()], category="week52_high", session_date=today)
    assert [d.kind for d in first] == ["new"]
    assert second == []                       # the whole point of debounce


def test_escalation_when_value_moves_past_trigger(dao: Dao):
    sm = AlertStateMachine(make_cfg(), dao)
    today = date(2026, 8, 28)
    sm.process([_sig(value=100.0)], category="week52_high", session_date=today)
    small = sm.process([_sig(value=102.0)], category="week52_high",
                       session_date=today)          # +2%, below 5% threshold
    big = sm.process([_sig(value=106.0)], category="week52_high",
                     session_date=today)            # +6% from trigger
    assert small == []
    assert [d.kind for d in big] == ["escalation"]


def test_reminder_after_configured_hours(dao: Dao):
    sm = AlertStateMachine(make_cfg(), dao)
    today = date(2026, 8, 28)
    sm.process([_sig()], category="week52_high", session_date=today)
    # Backdate the notification to simulate time passing.
    key = event_key("week52_high", "fresh_extreme", "ACME", "week52_high")
    dao.upsert_event_state(
        event_key=key,
        last_notified_at=datetime.now(timezone.utc) - timedelta(hours=4),
    )
    out = sm.process([_sig()], category="week52_high", session_date=today)
    assert [d.kind for d in out] == ["reminder"]
    # ...and only once, because max_reminders is 1.
    assert sm.process([_sig()], category="week52_high",
                      session_date=today) == []


def test_state_closes_then_can_refire(dao: Dao):
    sm = AlertStateMachine(make_cfg(), dao)   # close_after_missed_polls = 2
    today = date(2026, 8, 28)
    sm.process([_sig()], category="week52_high", session_date=today)
    for _ in range(2):
        sm.process([], category="week52_high", session_date=today)
    again = sm.process([_sig()], category="week52_high", session_date=today)
    assert [d.kind for d in again] == ["new"]


def test_per_symbol_daily_cap(dao: Dao):
    cfg = make_cfg(**{"rules.debounce.max_alerts_per_symbol_per_day": 2})
    sm = AlertStateMachine(cfg, dao)
    today = date(2026, 8, 28)
    emitted = 0
    for i in range(5):
        out = sm.process([_sig(bucket=f"deal{i}")], category="week52_high",
                         session_date=today)
        for d in out:                          # dispatcher normally does this
            dao.record_alert(event_key=d.event_key, category="week52_high",
                             rule_id="fresh_extreme", entity="ACME",
                             severity="notable", kind=d.kind, title="t",
                             trade_date=today)
            emitted += 1
    assert emitted == 2


def test_intraday_states_reset_at_session_open(dao: Dao):
    sm = AlertStateMachine(make_cfg(), dao)
    sm.process([_sig()], category="week52_high",
               session_date=date(2026, 8, 27))
    sm.reset_session(date(2026, 8, 28))
    out = sm.process([_sig()], category="week52_high",
                     session_date=date(2026, 8, 28))
    assert [d.kind for d in out] == ["new"]
