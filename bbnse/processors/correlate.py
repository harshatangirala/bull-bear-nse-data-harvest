"""Cross-feed correlation.

NSE publishes overlapping data. The same real-world event reaches us through
several feeds, and without correlation you get one alert per feed:

* A block deal above the bulk threshold is published in *both* the block and
  bulk feeds -- one trade, two rows, in a single payload.
* A stock moving hard appears in gainers, in volume spurts, and at its price
  band -- one move, three categories, three separate poll cycles.
* A contract with an OI surge appears in OI spurts, the derivatives watch and
  most-active contracts.

The mechanism is one thing, not three special cases. A processor stamps each
Signal with a `dedup_key` describing the underlying event independently of
which feed found it. Signals sharing a key, inside the same correlation group
and within `window_minutes`, collapse to a single alert: the highest
`dedup_priority` becomes the headline and the rest are recorded as
corroborations, surfaced on the alert as "also in: <categories>".

Two scopes are handled together:

  within one batch   both rows arrive in one payload (bulk/block deals)
  across cycles      different fetchers, minutes apart (gainers vs spurts)

Groups are declared in config.yaml, so adding a Phase-2 category to an
existing overlap is a config edit, not a code change.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from ..core.logging_setup import get_logger
from ..storage.dao import Dao
from .base import SEVERITY_ORDER, Signal

log = get_logger(__name__)


def make_dedup_key(group: str, *parts) -> str:
    """Stable identity for an event, safe to use as a DB key."""
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
    return f"{group}:{digest}"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class CrossFeedDeduplicator:
    def __init__(self, cfg, dao: Dao):
        self.cfg = cfg
        self.dao = dao
        c = cfg.section("rules.cross_feed_dedup")
        self.enabled = bool(c.get("enabled", True))
        self.window = timedelta(minutes=float(c.get("window_minutes", 30)))
        self.escalate_on_higher = bool(c.get("escalate_on_higher_severity",
                                             True))
        self.escalate_on_priority = bool(c.get("escalate_on_higher_priority",
                                               True))
        # category -> group name
        self._group_of: dict[str, str] = {}
        for group, categories in (c.get("groups") or {}).items():
            for category in categories or []:
                self._group_of[category] = group

    def group_for(self, category: str) -> str | None:
        return self._group_of.get(category)

    def apply(self, signals: list[Signal], *,
              session_date: date | None = None) -> list[Signal]:
        """Collapse signals describing the same event. Returns survivors."""
        if not self.enabled or not signals:
            return signals

        session_date = session_date or datetime.now(timezone.utc).date()
        now = datetime.now(timezone.utc)

        # Signals without a key, or in no configured group, pass through
        # untouched -- correlation is strictly opt-in.
        keyed: list[Signal] = []
        passthrough: list[Signal] = []
        for sig in signals:
            group = self.group_for(sig.category)
            if not sig.dedup_key or group is None:
                passthrough.append(sig)
            else:
                sig.dedup_group = group
                keyed.append(sig)

        if not keyed:
            return signals

        # Highest priority first so the most specific feed becomes the
        # headline; ties broken by severity, then by size of the number.
        keyed.sort(key=lambda s: (-s.dedup_priority,
                                  -SEVERITY_ORDER.get(s.severity, 0),
                                  -abs(s.value or 0)))

        survivors: list[Signal] = []
        batch_winner: dict[str, Signal] = {}
        suppressed = 0

        for sig in keyed:
            key = f"{sig.dedup_group}:{sig.dedup_key}"

            # --- collision inside this batch (e.g. bulk vs block) -----------
            winner = batch_winner.get(key)
            if winner is not None:
                if sig.feed_label not in winner.also_in:
                    winner.also_in.append(sig.feed_label)
                self.dao.add_corroboration(key, sig.feed_label, sig.severity,
                                           sig.dedup_priority)
                suppressed += 1
                continue

            # --- collision with an earlier cycle ---------------------------
            corr = self.dao.get_correlation(key)
            if corr is not None:
                first_seen = _aware(corr.first_seen_at)
                if first_seen is not None and now - first_seen <= self.window:
                    worse = (SEVERITY_ORDER.get(sig.severity, 0)
                             > SEVERITY_ORDER.get(corr.top_severity, 0))
                    # A more specific feed outranking the incumbent is also
                    # new information even at equal severity: "locked at its
                    # circuit" says something "up 20%" does not.
                    outranks = (self.escalate_on_priority
                                and sig.dedup_priority > (corr.top_priority or 0))
                    higher = worse or outranks
                    self.dao.add_corroboration(key, sig.feed_label,
                                               sig.severity,
                                               sig.dedup_priority)
                    if self.escalate_on_higher and higher:
                        # A corroborating feed reporting something *worse*
                        # (a mover that then hits its circuit) is genuinely
                        # new information, so it is allowed through.
                        sig.also_in = sorted(
                            {corr.first_category, *(corr.corroborations or [])}
                            - {sig.feed_label})
                        sig.body = self._annotate(sig)
                        survivors.append(sig)
                        batch_winner[key] = sig
                        log.info("corroborating feed escalated severity",
                                 extra={"entity": sig.entity,
                                        "category": sig.category,
                                        "was": corr.top_severity,
                                        "now": sig.severity})
                        continue
                    suppressed += 1
                    log.debug("suppressed duplicate across feeds",
                              extra={"entity": sig.entity,
                                     "category": sig.category,
                                     "first_seen_in": corr.first_category})
                    continue
                # Outside the window the old correlation is stale: this is a
                # genuinely new occurrence, so reset it rather than treating
                # this feed as a corroboration of a hours-old event.
                self.dao.reopen_correlation(
                    key, category=sig.feed_label, severity=sig.severity,
                    session_date=session_date, priority=sig.dedup_priority,
                )
            else:
                self.dao.open_correlation(
                    dedup_key=key, dedup_group=sig.dedup_group or "",
                    category=sig.feed_label, entity=sig.entity,
                    severity=sig.severity, session_date=session_date,
                    priority=sig.dedup_priority,
                )

            survivors.append(sig)
            batch_winner[key] = sig

        # Batch winners may have gained corroborations after being appended;
        # re-render their bodies so the tag is present.
        for sig in survivors:
            if sig.also_in:
                sig.body = self._annotate(sig)

        if suppressed:
            log.info("cross-feed duplicates collapsed",
                     extra={"suppressed": suppressed,
                            "emitted": len(survivors)})

        return survivors + passthrough

    @staticmethod
    def _annotate(sig: Signal) -> str:
        """Append the 'also in' tag without duplicating it on re-render."""
        base = sig.body.split(" | also in: ")[0]
        if not sig.also_in:
            return base
        return f"{base} | also in: {', '.join(sorted(set(sig.also_in)))}"
