# Bull Bear NSE Data Harvest

Monitors NSE India's market-data feeds, alerts on genuinely important events
as they happen, and rolls everything up into daily, weekly and monthly reports.

**Status: complete.** All 25 categories run end-to-end (fetch → store →
detect → notify → report) — the original 20 plus 5 covering the 3
categories once thought to need browser automation to resolve (they didn't;
see [Roadmap](#roadmap)). Includes cross-feed deduplication and a full
unit-conversion audit against live samples.

---

## Quick start

```bash
cd bull-bear-nse-data-harvest
python -m pip install -r requirements.txt

python main.py verify-endpoints          # confirm NSE is reachable
python main.py universe                  # build the alert universe
python main.py once --all --dry-run      # one cycle, nothing dispatched
python main.py once --all                # for real (console alerts)
python main.py report daily              # generate today's report
python main.py run                       # long-running scheduler
```

Requires Python 3.11+ (developed on 3.12).

---

## Dashboard

```bash
streamlit run dashboard.py
```

Opens a browser tab with filterable alert history, fetcher health, large
deals, and market-breadth views, reading directly from the SQLite database.
It is read-only and safe to run alongside `python main.py run` — SQLite's
WAL mode (already on) lets a reader see committed rows without blocking the
poller's writes. Data auto-refreshes every 15s from cache, or click
**Refresh now** for an immediate pull. Works even before the first
`python main.py once --all` — it shows an empty-state message instead of
crashing.

---

## What it does

**Alerts, one at a time, as detected.** Never batched. A stock that sits at a
52-week high all afternoon produces exactly one alert, not one per poll — see
[Debounce](#how-debounce-works).

**Reports on a schedule.** Daily after close, weekly on Saturday morning,
monthly on the 1st. Markdown + standalone HTML, optionally pushed to Telegram
as a document.

**Everything stored.** Raw payloads (gzipped, deduped) plus normalized rows, so
any past report can be regenerated and you can query history yourself:

```bash
sqlite3 data/bbnse.sqlite3 \
  "select symbol, sum(value_cr) v from deal_observation
   where trade_date >= '2026-08-01' group by 1 order by v desc limit 10;"
```

---

## Architecture

```
main.py                CLI
config.yaml            every threshold and cadence knob
endpoints.yaml         endpoint registry (no URLs in Python)

bbnse/
  core/
    session.py         cookie bootstrap, refresh, backoff, jitter
    registry.py        endpoints.yaml loader + row extraction
    calendar.py        trading calendar from NSE's own holiday API
    universe.py        which symbols may alert
    health.py          fetcher failure / staleness tracking
    config.py          config + .env loading
    logging_setup.py   console + JSONL structured logs
  fetchers/            one module per category  -> normalized rows
  processors/          one module per category  -> Signals
    state.py           the debounce state machine
  notifiers/           console, telegram (+ email/discord hooks)
  storage/             SQLAlchemy models + all SQL
  reports/             daily / weekly / monthly
  scheduler/runner.py  APScheduler wiring + catch-up
  pipeline.py          fetch -> store -> detect -> notify
```

The separation that matters: **fetchers never decide importance, processors
never decide whether to notify.** Processors emit Signals ("this is
interesting"); the state machine decides whether that Signal becomes an alert
("...and you have not been told yet"). That split is what makes debouncing
work and what lets reports be rebuilt from history.

### Adding a category

Three steps, no changes anywhere else:

1. Add the endpoint to `endpoints.yaml` (29 are already documented there).
2. Add a `Fetcher` subclass implementing `normalize()`.
3. Add a `Processor` subclass implementing `evaluate()`, plus a `rules:` block
   in `config.yaml`.

Then register it in `_CATEGORIES` in `pipeline.py`.

---

## NSE-specific handling

Full detail in [`docs/endpoints.md`](docs/endpoints.md). In brief:

- Bare `requests` gets **403 in 90 ms** from Akamai. A full browser header set
  gets 200 plus the bot cookies. Handled in `core/session.py`.
- Cookies expire; the session re-bootstraps automatically on 401/403 and after
  `http.session_max_age_sec`.
- Every request sleeps a random `http.jitter_sec` interval; retries use
  exponential backoff with decorrelating jitter.
- **NSE runs two API generations simultaneously** (`/api/...` and the newer
  `/api/NextApi/...` gateway) and migrates pages between them without notice.
  This is why endpoints live in YAML. Run `python main.py verify-endpoints`
  periodically — a `[FAIL]` row usually means a page was migrated.
- Polling cadence is per-tier and gated on NSE's live trading calendar, so
  holidays and weekends cost zero requests.

---

## Configuration

### Thresholds

All in `config.yaml`; no code changes needed. Every rule has an `enabled` flag
and a severity of `info` / `notable` / `critical`.

| Knob | Default | Effect |
|---|---|---|
| `rules.gainers_losers.pct_move_notable` | 5.0 | % move to alert |
| `rules.gainers_losers.pct_move_critical` | 9.0 | % move for critical |
| `rules.gainers_losers.min_traded_value_cr` | 5.0 | drop illiquid noise |
| `rules.week52.min_ltp` | 20.0 | ignore penny stocks |
| `rules.week52.breakout_margin_pct_critical` | 3.0 | wide breakout = critical |
| `rules.large_deals.value_cr_notable` | 10.0 | bulk deal floor (₹ cr) |
| `rules.large_deals.value_cr_critical` | 50.0 | critical deal size |
| `rules.large_deals.block_value_cr_notable` | 25.0 | separate block-deal floor |
| `rules.large_deals.watch_clients` | `[]` | alert on these names at any size |

### Alert universe

`universe.mode` controls which symbols may alert. Reports always cover the
whole market regardless.

| Mode | Symbols | Notes |
|---|---|---|
| `fo_plus_nifty500` | ~507 | **default** — liquid names only |
| `nifty500` | 500 | |
| `all` | ~2559 | includes SME; expect far more noise |
| `watchlist` | your list | edit `universe/watchlist.txt` |

`always_include` / `never_include` override any mode.

### How debounce works

Configured under `rules.debounce`. Every signal maps to a stable
`event_key = (category, rule, symbol, state_bucket)` with open/closed state in
the DB. An alert fires only on:

| Kind | When |
|---|---|
| `new` | the key was absent or previously closed |
| `escalation` | value moved `escalate_on_pct_move` (5%) past where it first fired |
| `reminder` | still true after `remind_after_hours` (3), capped at `max_reminders` (1) |

Everything else is silent. Keys unseen for `close_after_missed_polls` cycles
are closed so the condition can legitimately re-fire later, and intraday states
reset at each session open.

`max_alerts_per_symbol_per_day` (default 8) caps any single symbol — one
heavily-churned name can otherwise dominate the stream. On 28-Aug-2026,
MVELECTRO had 38 qualifying bulk deals from prop desks; the cap keeps that to 8
alerts while the report still lists all 38 rows.

Verified end-to-end: running the same cycle twice produced **194 signals both
times, 194 alerts then 0**.

---

## Telegram setup

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Message your new bot once (bots cannot start conversations).
3. Get your chat id:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → `result[0].message.chat.id`
4. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
5. In `config.yaml` set `notifiers.telegram.enabled: true`.
6. Test: `python main.py test-alert`

`min_severity` filters what reaches Telegram (default `notable`, so `info`
never pushes). `rate_limit_per_minute` (default 20) is a hard ceiling so a bad
data day cannot spam you.

### Adding another channel

Implement `Notifier` in `bbnse/notifiers/`, register it in the `_REGISTRY`
dict in `dispatch.py`, add a stanza under `notifiers:` in `config.yaml`.
Nothing else in the codebase knows which channels exist.

---

## Running as a service

`python main.py run` starts the scheduler in the foreground. It is built for an
**intermittently-on laptop**:

- Daily/EOD jobs missed while the machine was off are **backfilled on startup**
  (`schedule.catchup`, 5 trading days back). Intraday gaps are logged but not
  backfilled — NSE does not serve historical intraday snapshots.
- Every job re-checks the trading calendar before doing work.

To auto-start on Windows, create a Task Scheduler entry that runs at 08:45 IST
on weekdays:

```powershell
schtasks /Create /TN "BullBearNSE" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:45 `
  /TR "cmd /c cd /d C:\Users\harsh\bull-bear-nse-data-harvest && python main.py run" `
  /RL LIMITED /F
```

---

## Commands

| Command | Purpose |
|---|---|
| `main.py status` | table counts + fetcher health |
| `main.py verify-endpoints` | re-probe every registry endpoint |
| `main.py universe [--refresh]` | show/rebuild the alert universe |
| `main.py once <cat...> \| --all` | one cycle; `--dry-run`, `--force` |
| `main.py report daily\|weekly\|monthly` | `--date`, `--print`, `--send` |
| `main.py test-alert` | one synthetic alert through the real path |
| `main.py run` | long-running scheduler |

---

## Monitoring and troubleshooting

Structured JSONL logs land in `logs/bbnse.jsonl`:

```bash
python -c "import json;[print(json.loads(l)['msg']) for l in open('logs/bbnse.jsonl',encoding='utf-8') if json.loads(l)['level']=='ERROR']"
```

Health checks run every 30 minutes and alert when a fetcher fails
`health.consecutive_failure_alert` times running, or goes
`health.staleness_alert_minutes` without a successful fetch.

| Symptom | Cause / fix |
|---|---|
| `[FAIL]` in `verify-endpoints` | NSE migrated the page. Re-discover per `docs/endpoints.md` §3, update `endpoints.yaml`. |
| Persistent 403s on API calls | Akamai tightened on TLS fingerprint. `pip install curl-cffi`, set `http.backend: curl_cffi`. |
| No alerts at all | Check `main.py universe` is non-empty and the market window in `main.py once` output is `market_hours`. |
| Too many alerts | Raise `pct_move_notable` / `value_cr_notable`, or lower `max_alerts_per_symbol_per_day`. |
| Garbled console characters | Non-UTF-8 terminal. Handled automatically, but `set PYTHONIOENCODING=utf-8` if piping. |

---

## Tests

```bash
python -m pytest tests/ -q     # 173 tests, no network required
```

Covers the state machine contract (fire-once, escalation, reminder, close and
re-fire, per-symbol cap, session reset), every verified unit conversion
(lakh/crore/rupees/lakh-shares, each pinned to a live-sample number), the
signed 52-week margin, the general cross-feed deduplicator (same-batch and
cross-cycle collisions, severity/priority breakthrough, config-driven
groups), and order-insensitive snapshot hashing. Each Phase 2 category has
its own test module (`test_phase2_*.py`) built on payload fragments copied
verbatim from live NSE responses.

---

## Defaults chosen for you

| Decision | Default | Why |
|---|---|---|
| Alert universe | F&O + Nifty 500 (~507) | Illiquid microcaps generate most false spurts |
| Large deals ignore the universe | `respect_universe: false` | A ₹80 cr deal in a smallcap is exactly what you want to hear about |
| Block deals get a higher floor | 25 vs 10 ₹ cr | Block deals are structurally larger |
| Storage | SQLite + WAL | Report generator reads while the poller writes |
| Raw retention | 30 days | Normalized rows kept 400 days; reports never read raw |
| Timezone | IST everywhere | Timestamps stored UTC, displayed IST |
| Severity floor for Telegram | `notable` | `info` stays in the DB and reports only |

---

## Roadmap

**Phase 1 (done)** — gainers, losers, 52-week high/low, large deals.

**Phase 2 (done)** — the remaining 15 categories: volume spurts, price band
hitters, advance/decline, most active (value), indices, new listings, ETFs,
pre-open CM & F&O, OI spurts, derivatives watch, most active contracts,
option chain, FII/DII, daily reports. All 20 are enabled by default in
`config.yaml`.

Two things worth knowing before extending further:

- **Cross-feed deduplication is general, not per-pair.** Any processor can
  opt a `Signal` into a named group (`config.yaml` →
  `rules.cross_feed_dedup.groups`) and the shared `CrossFeedDeduplicator`
  (`processors/correlate.py`) collapses same-event duplicates across feeds —
  within one poll (bulk/block) or across cycles (gainers → volume spurt →
  price band). Adding a new overlap is a config edit.
- **Not every unit could be verified.** `most_active_contracts.totalTurnover`
  and `option_chain.openInterest` have no reconciling conversion found
  against live samples (option_chain's endpoint has returned an empty
  Akamai-cached `{}` on every attempt so far). Both rules are built to not
  need the unverified field — see `docs/endpoints.md` §7 before touching
  either.

**Phase 3 (done)** — GSM, ASM, surveillance price-band changes, SLB, and the
Closing Auction Session. These were originally marked unresolved on the
assumption they were legacy Drupal pages with no client-side data loading;
that assumption was wrong — a discovery-script bug (matching script `src=`
literally ending in `.js`, which every real page-specific bundle here
defeats with a `?v=` cache-busting suffix) was hiding the real endpoints.
Re-scanning without that filter found all five in one pass. See
`docs/endpoints.md` §8 for the full writeup, including the one endpoint
(Closing Auction Session) whose unit was confirmed by reading the frontend's
own conversion code rather than by live arithmetic, since it returns no
rows outside its narrow pre-close window.

**Known limitations, not blockers:**
- `most_active_contracts.totalTurnover` and `option_chain.openInterest`
  have no reconciling unit conversion found against live samples (see
  `docs/endpoints.md` §7). Both rules are built to not need the unverified
  field.
- `option_chain` has returned an Akamai-cached empty `{}` on every attempt
  regardless of query string or cache-control headers — confirmed not
  something the client side can route around. The fetcher degrades to zero
  rows safely.
- `slb.turnOver` / `slb.transactionValue` are similarly unverified (every
  live row so far had zero volume) and are not used by the rule.
