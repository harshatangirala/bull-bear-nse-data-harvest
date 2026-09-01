"""Alert state machine.

The requirement is "alert on the transition, not on the condition". A stock
that stays at a 52-week high all afternoon is one alert, not one per poll.

Every signal maps to a stable event_key. The machine emits an alert only when:

  new         the key was absent or previously closed
  escalation  the value has moved `escalate_on_pct_move` past where it first
              fired -- a stock that breaks out and then runs another 5% is
              genuinely new information
  reminder    still true after `remind_after_hours`, capped at `max_reminders`

Everything else is silent. Keys unseen for `close_after_missed_polls`
consecutive cycles are closed, so the condition can legitimately re-fire
later. Intraday states are reset at each session open.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from ..core.logging_setup import get_logger
from ..storage.dao import Dao
from .base import Signal

log = get_logger(__name__)


def event_key(category: str, rule_id: str, entity: str,
              state_bucket: str) -> str:
    raw = f"{category}|{rule_id}|{entity}|{state_bucket}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


@dataclass
class AlertDecision:
    signal: Signal
    kind: str            # new | escalation | reminder
    event_key: str
    previous_value: float | None = None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class AlertStateMachine:
    def __init__(self, cfg, dao: Dao):
        self.cfg = cfg
        self.dao = dao
        d = cfg.section("rules.debounce")
        self.remind_after_hours = float(d.get("remind_after_hours", 3) or 0)
        self.max_reminders = int(d.get("max_reminders", 1))
        self.escalate_pct = float(d.get("escalate_on_pct_move", 0) or 0)
        self.close_after_missed = int(d.get("close_after_missed_polls", 3))
        self.reset_at_open = bool(d.get("reset_intraday_at_open", True))
        # 0 disables the cap. Guards against one heavily-traded name (prop
        # desks churning bulk deals, say) monopolising the alert stream.
        self.max_per_symbol_day = int(d.get("max_alerts_per_symbol_per_day", 0))

    def reset_session(self, session_date: date) -> int:
        if not self.reset_at_open:
            return 0
        n = self.dao.reset_intraday_states(session_date)
        if n:
            log.info("intraday states reset for new session",
                     extra={"closed": n, "session_date": str(session_date)})
        return n

    def process(self, signals: list[Signal], *, category: str,
                session_date: date | None = None) -> list[AlertDecision]:
        now = datetime.now(timezone.utc)
        session_date = session_date or now.date()
        decisions: list[AlertDecision] = []
        seen_keys: set[str] = set()

        # One batched lookup rather than a query per signal.
        budget: dict[str, int] = {}
        if self.max_per_symbol_day > 0 and signals:
            already = self.dao.alert_counts_for_entities(
                sorted({s.entity for s in signals}), session_date
            )
            budget = {e: self.max_per_symbol_day - n
                      for e, n in already.items()}
        capped: set[str] = set()

        for sig in signals:
            key = event_key(sig.category, sig.rule_id, sig.entity,
                            sig.state_bucket)
            seen_keys.add(key)
            st = self.dao.get_event_state(key)

            # --- daily per-symbol budget -------------------------------------
            if self.max_per_symbol_day > 0:
                remaining = budget.get(sig.entity, self.max_per_symbol_day)
                if remaining <= 0:
                    # Record the state so it is not re-evaluated forever, but
                    # do not emit. The report still shows every underlying row.
                    capped.add(sig.entity)
                    self.dao.upsert_event_state(
                        event_key=key, category=sig.category,
                        rule_id=sig.rule_id, entity=sig.entity,
                        state_bucket=sig.state_bucket, state="open",
                        first_seen_at=now, last_seen_at=now, closed_at=None,
                        last_notified_at=now, notify_count=0,
                        reminder_count=0, missed_polls=0,
                        trigger_value=sig.value, last_value=sig.value,
                        session_date=session_date,
                    )
                    continue

            # --- first sighting, or the condition had lapsed and returned ---
            if st is None or st.state == "closed":
                budget[sig.entity] = budget.get(
                    sig.entity, self.max_per_symbol_day) - 1
                self.dao.upsert_event_state(
                    event_key=key, category=sig.category, rule_id=sig.rule_id,
                    entity=sig.entity, state_bucket=sig.state_bucket,
                    state="open", first_seen_at=now, last_seen_at=now,
                    closed_at=None, last_notified_at=now, notify_count=1,
                    reminder_count=0, missed_polls=0,
                    trigger_value=sig.value, last_value=sig.value,
                    session_date=session_date,
                )
                decisions.append(AlertDecision(sig, "new", key))
                continue

            # --- already open: decide between escalate / remind / silence ---
            prev_value = st.last_value
            fields = {
                "event_key": key, "last_seen_at": now,
                "last_value": sig.value, "missed_polls": 0,
            }
            kind = None

            if (self.escalate_pct > 0 and sig.value is not None
                    and st.trigger_value):
                moved = abs(sig.value - st.trigger_value)
                base = abs(st.trigger_value)
                if base > 0 and (moved / base) * 100.0 >= self.escalate_pct:
                    kind = "escalation"
                    fields.update(trigger_value=sig.value,
                                  last_notified_at=now,
                                  notify_count=st.notify_count + 1)

            if kind is None and self.remind_after_hours > 0:
                last_note = _aware(st.last_notified_at) or _aware(st.first_seen_at)
                due = (last_note is not None and
                       now - last_note >= timedelta(hours=self.remind_after_hours))
                if due and st.reminder_count < self.max_reminders:
                    kind = "reminder"
                    fields.update(last_notified_at=now,
                                  reminder_count=st.reminder_count + 1,
                                  notify_count=st.notify_count + 1)

            self.dao.upsert_event_state(**fields)
            if kind:
                decisions.append(AlertDecision(sig, kind, key, prev_value))

        closed = self.dao.close_stale_states(category, seen_keys,
                                             self.close_after_missed)
        if closed:
            log.debug("states closed", extra={"category": category,
                                              "closed": closed})
        if capped:
            log.info("per-symbol daily alert cap reached",
                     extra={"category": category,
                            "symbols": sorted(capped)[:10],
                            "cap": self.max_per_symbol_day})
        if decisions:
            log.info("alerts to send",
                     extra={"category": category, "count": len(decisions),
                            "suppressed": len(signals) - len(decisions)})
        return decisions
