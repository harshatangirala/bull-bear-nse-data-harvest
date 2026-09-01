"""Cross-feed correlation.

The mechanism must be general: it is exercised here with deal feeds, equity
feeds and derivatives feeds through the same code path, because the whole
point of the refactor was that overlaps are not special-cased per pair.
"""
from __future__ import annotations

from datetime import date, timedelta

from bbnse.processors.base import Signal
from bbnse.processors.correlate import CrossFeedDeduplicator, make_dedup_key
from bbnse.storage.models import utcnow

from .helpers import make_cfg

TODAY = date(2026, 8, 28)


def sig(category, entity, *, key_parts=("ACME",), group="equity_move",
        severity="notable", priority=0, label=None, value=1.0, body="b"):
    return Signal(
        category=category, rule_id="r", entity=entity,
        state_bucket="s", severity=severity, title=f"{entity} {category}",
        body=body, value=value,
        dedup_key=make_dedup_key(group, *key_parts),
        dedup_priority=priority, dedup_label=label,
    )


# --------------------------------------------------------------------------
# opt-in behaviour
# --------------------------------------------------------------------------
def test_signals_without_a_key_pass_through(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    s = Signal(category="gainers", rule_id="r", entity="ACME",
               state_bucket="s", severity="notable", title="t")
    assert d.apply([s], session_date=TODAY) == [s]


def test_category_outside_any_group_passes_through(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    # week52_high is deliberately not in a group.
    s = sig("week52_high", "ACME")
    out = d.apply([s], session_date=TODAY)
    assert out == [s]


def test_disabled_deduplicator_is_a_no_op(dao):
    d = CrossFeedDeduplicator(
        make_cfg(**{"rules.cross_feed_dedup.enabled": False}), dao)
    signals = [sig("gainers", "ACME"), sig("volume_spurts", "ACME")]
    assert len(d.apply(signals, session_date=TODAY)) == 2


# --------------------------------------------------------------------------
# same-batch collision (bulk vs block deals arrive in one payload)
# --------------------------------------------------------------------------
def test_same_batch_duplicates_collapse_to_highest_priority(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    bulk = sig("large_deals", "ATHERENERG", key_parts=("ATHERENERG", "HERO"),
               group="deals", priority=1, label="bulk feed")
    block = sig("large_deals", "ATHERENERG", key_parts=("ATHERENERG", "HERO"),
                group="deals", priority=2, label="block feed")
    out = d.apply([bulk, block], session_date=TODAY)
    assert len(out) == 1
    assert out[0].dedup_label == "block feed"      # higher priority wins
    assert out[0].also_in == ["bulk feed"]
    assert "also in: bulk feed" in out[0].body


def test_distinct_deals_in_one_batch_both_survive(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    a = sig("large_deals", "ACME", key_parts=("ACME", "FUND_A"),
            group="deals", priority=1, label="bulk feed")
    b = sig("large_deals", "ACME", key_parts=("ACME", "FUND_B"),
            group="deals", priority=1, label="bulk feed")
    assert len(d.apply([a, b], session_date=TODAY)) == 2


def test_annotation_is_not_duplicated_on_re_render(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    a = sig("gainers", "ACME", priority=2, body="LTP 100")
    b = sig("volume_spurts", "ACME", priority=1)
    out = d.apply([a, b], session_date=TODAY)
    assert out[0].body.count("also in:") == 1


# --------------------------------------------------------------------------
# cross-cycle collision (different fetchers, minutes apart)
# --------------------------------------------------------------------------
def test_second_feed_in_window_is_suppressed(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    first = d.apply([sig("gainers", "ACME")], session_date=TODAY)
    second = d.apply([sig("volume_spurts", "ACME")], session_date=TODAY)
    assert len(first) == 1
    assert second == []                       # same move, already alerted


def test_corroboration_is_recorded_even_when_suppressed(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    d.apply([sig("gainers", "ACME")], session_date=TODAY)
    d.apply([sig("volume_spurts", "ACME")], session_date=TODAY)
    key = f"equity_move:{make_dedup_key('equity_move', 'ACME')}"
    corr = dao.get_correlation(key)
    assert corr is not None
    assert corr.first_category == "gainers"
    assert "volume_spurts" in corr.corroborations


def test_different_groups_do_not_cross_contaminate(dao):
    """Same entity, same key parts, different group -> independent events."""
    d = CrossFeedDeduplicator(make_cfg(), dao)
    a = d.apply([sig("gainers", "ACME", group="equity_move")],
                session_date=TODAY)
    b = d.apply([sig("oi_spurts", "ACME", key_parts=("ACME",),
                     group="derivatives")], session_date=TODAY)
    assert len(a) == 1 and len(b) == 1


def test_occurrence_outside_the_window_refires(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    d.apply([sig("gainers", "ACME")], session_date=TODAY)

    # Age the correlation past the 30-minute window.
    key = f"equity_move:{make_dedup_key('equity_move', 'ACME')}"
    with dao.session() as s:
        from bbnse.storage.models import EventCorrelation
        from sqlalchemy import select
        corr = s.scalar(select(EventCorrelation)
                        .where(EventCorrelation.dedup_key == key))
        corr.first_seen_at = utcnow() - timedelta(hours=2)

    out = d.apply([sig("volume_spurts", "ACME")], session_date=TODAY)
    assert len(out) == 1                      # genuinely a new occurrence


def test_stale_correlation_is_reset_not_corroborated(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    d.apply([sig("gainers", "ACME")], session_date=TODAY)
    key = f"equity_move:{make_dedup_key('equity_move', 'ACME')}"
    with dao.session() as s:
        from bbnse.storage.models import EventCorrelation
        from sqlalchemy import select
        corr = s.scalar(select(EventCorrelation)
                        .where(EventCorrelation.dedup_key == key))
        corr.first_seen_at = utcnow() - timedelta(hours=2)

    d.apply([sig("volume_spurts", "ACME")], session_date=TODAY)
    corr = dao.get_correlation(key)
    assert corr.first_category == "volume_spurts"   # ownership moved
    assert corr.corroborations == []                # history cleared


# --------------------------------------------------------------------------
# severity escalation
# --------------------------------------------------------------------------
def test_higher_severity_corroboration_breaks_through(dao):
    """A mover that then hits its circuit is genuinely new information."""
    d = CrossFeedDeduplicator(make_cfg(), dao)
    d.apply([sig("gainers", "ACME", severity="notable")], session_date=TODAY)
    out = d.apply([sig("price_band_hitters", "ACME", severity="critical")],
                  session_date=TODAY)
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert "gainers" in out[0].also_in
    assert "also in: gainers" in out[0].body


def test_equal_severity_corroboration_stays_suppressed(dao):
    d = CrossFeedDeduplicator(make_cfg(), dao)
    d.apply([sig("gainers", "ACME", severity="notable")], session_date=TODAY)
    out = d.apply([sig("volume_spurts", "ACME", severity="notable")],
                  session_date=TODAY)
    assert out == []


def test_escalation_can_be_disabled(dao):
    d = CrossFeedDeduplicator(
        make_cfg(**{"rules.cross_feed_dedup.escalate_on_higher_severity":
                    False}), dao)
    d.apply([sig("gainers", "ACME", severity="notable")], session_date=TODAY)
    out = d.apply([sig("price_band_hitters", "ACME", severity="critical")],
                  session_date=TODAY)
    assert out == []


def test_groups_are_config_driven_not_hardcoded(dao):
    """Moving a category between groups is a config edit, nothing more."""
    cfg = make_cfg(**{"rules.cross_feed_dedup.groups": {
        "deals": ["large_deals"],
        "equity_move": ["gainers"],          # volume_spurts removed
        "derivatives": ["volume_spurts"],    # ...and moved here
    }})
    d = CrossFeedDeduplicator(cfg, dao)
    assert d.group_for("volume_spurts") == "derivatives"
    out = d.apply([sig("gainers", "ACME"), sig("volume_spurts", "ACME")],
                  session_date=TODAY)
    assert len(out) == 2                      # no longer the same group
