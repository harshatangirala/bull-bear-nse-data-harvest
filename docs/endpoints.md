# NSE Endpoint Reference

Everything here was verified live against nseindia.com on **2026-08-29**
(a Saturday, so intraday feeds carry the Friday 28-Aug-2026 close).

Re-verify at any time:

```bash
python main.py verify-endpoints
```

---

## 1. Getting past the block

A bare `requests.get("https://www.nseindia.com/")` returns **403 in ~90 ms** —
that is the Akamai edge refusing the request, not a network problem. The fix is
a complete browser header set. With the headers below the same call returns
**200** and sets the bot cookies `_abck`, `ak_bmsc`, `bm_sz`, `AKA_A2`.

```python
{
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8",
  "Accept-Language": "en-US,en;q=0.9",
  "Accept-Encoding": "gzip, deflate",
  "Connection": "keep-alive",
  "Upgrade-Insecure-Requests": "1",
  "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
  "sec-ch-ua-mobile": "?0",
  "sec-ch-ua-platform": '"Windows"',
  "Sec-Fetch-Dest": "document",
  "Sec-Fetch-Mode": "navigate",
  "Sec-Fetch-Site": "none",
  "Sec-Fetch-User": "?1",
}
```

Then, for the API call itself, swap to XHR-shaped headers and set a `Referer`
matching the page that would normally issue the request:

```python
{
  ...common headers...,
  "Accept": "*/*",
  "Referer": "https://www.nseindia.com/market-data/top-gainers-losers",
  "X-Requested-With": "XMLHttpRequest",
  "Sec-Fetch-Dest": "empty",
  "Sec-Fetch-Mode": "cors",
  "Sec-Fetch-Site": "same-origin",
}
```

### Two quirks worth knowing

**The homepage sometimes 403s while the APIs still work.** In testing,
`requests` got 403 on the homepage but the API calls that followed returned
200, because `AKA_A2` was set anyway. `curl` with identical headers got 200.
The difference is the TLS fingerprint — `requests`/urllib3 does not look like
Chrome at the TLS layer. Akamai is not currently enforcing on that, but it
could start. `core/session.py` therefore treats a 403 bootstrap as a warning,
not a failure, and `http.backend: curl_cffi` in `config.yaml` is the escape
hatch (`pip install curl-cffi` for real Chrome TLS impersonation).

**Row ordering is not stable.** Consecutive polls of the same endpoint return
the same symbol set in a different order (confirmed on the `allSec` bucket of
`live-analysis-variations`). Hashing the raw payload therefore marks every poll
as changed and defeats snapshot dedup, so `storage/dao.py` canonicalises
(sorts) nested lists before hashing. The stored payload is never reordered.

---

## 2. NSE runs two API generations at once

This is the single most important structural fact about the site, and the
reason endpoints live in `endpoints.yaml` rather than in Python.

| Generation | Shape | Status |
|---|---|---|
| `legacy` | `/api/live-analysis-variations?index=gainers` | Still the majority |
| `nextapi` | `/api/NextApi/apiClient/marketWatchApi?functionName=getBlockDealsData` | Newer pages migrated here |
| `archive` | `https://nsearchives.nseindia.com/content/...csv` | Constituent lists, historical files |

NSE is migrating pages one at a time. This is why the widely-cited
`/api/equity-stockIndices?index=NIFTY%2050` now returns **404** — the
`live-equity-market` page moved to the `NextApi` gateway. Any project that
hardcodes one URL scheme will rot within months.

---

## 3. Discovery recipe

Three tiers, in order of preference.

**Tier 1 — grep the Next.js bundles.** Works for modern pages.

```python
# fetch the page, collect its JS chunks, grep them for /api/ strings
scripts = re.findall(r'src="(/_next/static/[^"]+\.js)"', page_html)
hits = set(re.findall(r'["\'`](/api/[A-Za-z0-9_\-/?=&%.{}$]+)', js_text))
```

Running this against `/market-data/live-equity-market` scanned 21 chunks and
surfaced 23 distinct `/api/` references — including the whole `NextApi`
gateway, which no amount of URL guessing would have found.

**Tier 2 — guess-and-probe.** NSE's legacy naming is consistent
(`live-analysis-*`, `snapshot-*`, `market-data-*`). 17 of the first 20 guesses
returned 200.

**Tier 3 — runtime network capture.** Needed for legacy Drupal pages that have
no JS chunks and no `__NEXT_DATA__`. Drive the page with Playwright and record
the XHRs:

```python
page.on("response", lambda r: print(r.url) if "/api/" in r.url else None)
```

---

## 4. Verified endpoints

All paths are relative to `https://www.nseindia.com`. "Rows" is what the
endpoint returned on 2026-08-29.

### Wired up

All 25 target categories from the original goal are fetching, storing,
detecting and alerting — the 20 from the original scope plus the 5 that
cover the 3 categories once marked unresolved (see §8).

| Category | Path | Params | Rows (2026-08-29) | Notes |
|---|---|---|---|---|
| Gainers | `/api/live-analysis-variations` | `index=gainers` | 85 | Bucketed response |
| Losers | `/api/live-analysis-variations` | `index=loosers` | 86 | **NSE spells it "loosers"** |
| 52-week high | `/api/live-analysis-data-52weekhighstock` | – | 132 | |
| 52-week low | `/api/live-analysis-data-52weeklowstock` | – | 84 | |
| Large deals | `/api/snapshot-capital-market-largedeal` | – | 389 | Three blocks in one payload |
| Volume spurts | `/api/live-analysis-volume-gainers` | – | 25 | `week1volChange` is a **multiple**, not a percent |
| Price band hitters | `/api/live-analysis-price-band-hitter` | – | 140 | |
| Advance/Decline | `/api/live-analysis-advance` | – | 1919 | 525 KB; only a summary row is persisted |
| Most active (value) | `/api/live-analysis-most-active-securities` | `index=value` | 20 | |
| All indices | `/api/allIndices` | – | 139 | Only `rules.indices.watch` names are evaluated |
| New listings | `/api/new-listing-today` | – | 0 | Returns JSON `null` on quiet days |
| ETFs | `/api/etf` | – | 349 | Every numeric field arrives as a string |
| Pre-open CM | `/api/market-data-pre-open` | `key=ALL` | 2072 | Heaviest feed in the project (2 MB) |
| Pre-open F&O | `/api/market-data-pre-open` | `key=FO` | 210 | |
| OI spurts | `/api/live-analysis-oi-spurts-underlyings` | – | 216 | `avgInOI` is mislabeled — see §7 |
| Derivatives watch | `/api/liveEquity-derivatives` | `index=nse50_fut` | 3 | |
| Most active contracts | `/api/snapshot-derivatives-equity` | `index=contracts&limit=20` | 20 | Turnover units unverified — see §7 |
| Option chain | `/api/option-chain-v3` | `type=Indices&symbol=NIFTY` | 0\* | OI unit unverified; rule uses PCR only — see §7 |
| FII/DII | `/api/fiidiiTradeReact` | – | 2 | Bare list, not `{data: [...]}` |
| Daily reports | `/api/daily-reports` | `key=favCapital` | 0\* | `{"msg": "no data found"}` outside publish hours |
| GSM | `/api/reportGSM` | – | 82 | Bare list; resolved in Phase 3, see §8 |
| ASM | `/api/reportASM` | – | 144+90 | `{longterm, shortterm}`; see §8 |
| Surveillance price bands | `/api/eqsurvactions` | – | 6 | Bare list; see §8 |
| SLB series master | `/api/live-analysis-slb-series-master` | – | – | Metadata only; supplies the current series key |
| SLB | `/api/live-analysis-slb` | `series=<key>` | 23 | `series` rolls over monthly; see §8 |
| Closing Auction Session | `/api/NextApi/apiClient/casApi` | `functionName=getCASData` | 0\* | Empty outside the pre-close CAS window; see §8 |

\* Empty both on the Saturday this table was captured and again when
re-checked Tuesday 2026-09-01 11:09 IST — confirmed as an Akamai edge cache
serving `{}` regardless of query string or `Cache-Control: no-cache`, not a
market-hours gate. Both fetchers already degrade to zero rows safely; no
code change needed, but option_chain's OI-unit verification (§7) stays
blocked until a call gets past that cache.

### Reference only (not polled)

| Category | Path | Params | Rows |
|---|---|---|---|
| Option chain meta | `/api/option-chain-contract-info` | `symbol=NIFTY` | – |
| Market status | `/api/marketStatus` | – | 10 |
| **Trading holidays** | `/api/holiday-master` | `type=trading` | 240 |

`holiday-master` drives the whole scheduler — it is why no holiday list is
hardcoded anywhere.

### New gateway

| Category | Path | Params |
|---|---|---|
| Block deals | `/api/NextApi/apiClient/marketWatchApi` | `functionName=getBlockDealsData` |
| Index data | `/api/NextApi/apiClient` | `functionName=getIndexData&type=All` |
| Market turnover | `/api/NextApi/apiClient` | `functionName=getMarketTurnoverSummary` |

### Archive files

| Purpose | URL | Rows |
|---|---|---|
| Nifty 500 | `nsearchives.../content/indices/ind_nifty500list.csv` | 500 |
| F&O underlyings | `nsearchives.../content/fo/fo_mktlots.csv` | 217 |
| All equities | `nsearchives.../content/equities/EQUITY_L.csv` | 2559 |

---

## 5. Response shapes (phase 1)

### `live-analysis-variations` (gainers/losers)

Keyed by index bucket, **not** a flat list. A symbol can appear in several
buckets, so the processor alerts once on the widest move.

Buckets: `NIFTY`, `BANKNIFTY`, `NIFTYNEXT50`, `SecGtr20`, `SecLwr20`,
`FOSec`, `allSec`.

```json
{
  "NIFTY": {
    "data": [{
      "symbol": "TCS", "series": "EQ",
      "open_price": 2272, "high_price": 2348.5, "low_price": 2263.3,
      "ltp": 2342, "prev_price": 2248.4, "net_price": 4.16,
      "trade_quantity": 4054069,
      "turnover": 94377.51,
      "perChange": 4.16,
      "ca_ex_dt": "15-Jul-2026",
      "ca_purpose": "Interim Dividend - Rs 12 Per Share"
    }],
    "timestamp": "28-Aug-2026 16:00:00"
  }
}
```

> **`turnover` is in lakh, not rupees or crore.** TCS: 4,054,069 shares near
> ₹2,342 ≈ ₹9.49 bn = ₹94,946 lakh, matching the reported 94,377. Every
> threshold in `config.yaml` is in **crore**, so the fetcher divides by 100.
> Getting this wrong makes `min_traded_value_cr` off by 100×.

`ca_purpose` is worth surfacing in alerts — a dividend or split ex-date
explains a lot of apparently dramatic moves.

### `live-analysis-data-52week{high,low}stock`

```json
{
  "high": 132,
  "timestamp": "28-Aug-2026 16:00:00",
  "data": [{
    "symbol": "3BBLACKBIO",
    "comapnyName": "3B Blackbio Dx Limited",
    "ltp": 1440.1, "change": 89.8, "pChange": 6.65,
    "new52WHL": 1463, "prev52WHL": 1441.5,
    "prevHLDate": "10-Jun-2026",
    "prevClose": "1350.3",
    "series": "EQ"
  }]
}
```

Two traps:
- **`comapnyName` is misspelled in the API.** Handled, not corrected.
- **`prevClose` is a string** while every neighbouring number is a float.

`new52WHL` / `prev52WHL` let you compute how far the new extreme cleared the
old one. The fetcher stores that as a **signed** `margin_pct` where positive
always means "more extreme in the expected direction", for highs and lows
alike — so the processor uses one comparison for both.

### `snapshot-capital-market-largedeal`

Three independent blocks in one payload:

```json
{
  "as_on_date": "28-Aug-2026",
  "BULK_DEALS_DATA":  [ ... ],
  "BLOCK_DEALS_DATA": [ ... ],
  "SHORT_DEALS_DATA": [ ... ]
}
```

Each deal row:

```json
{
  "symbol": "TEJASNET", "name": "Tejas Networks Limited",
  "clientName": "GRAVITON RESEARCH CAPITAL LLP",
  "buySell": "BUY",
  "qty": "906031",
  "watp": "560.07",
  "date": "28-Aug-2026",
  "remarks": "-"
}
```

Traps:
- **`qty` and `watp` are strings.** Deal value = `qty × watp / 1e7` crore.
- **`remarks` is sometimes `null`, sometimes `"-"`.**
- **Both sides of a trade are disclosed separately** (one BUY row, one SELL
  row), which is correct, not duplication.
- **A block deal large enough to cross the bulk threshold appears in *both*
  feeds.** On 28-Aug there were 10 such exact twins. This is not
  special-cased here: the processor stamps every deal signal with a
  `dedup_key` identifying the underlying trade and a `dedup_priority`
  (block > bulk), and the shared `CrossFeedDeduplicator`
  (`processors/correlate.py`) collapses duplicates for *any* configured
  group — the same mechanism also covers gainers/volume-spurts/price-band
  and the derivatives feeds. Groups are declared in `config.yaml` under
  `rules.cross_feed_dedup.groups`, so adding a new overlap is a config
  edit, not a code change.
- **`SHORT_DEALS_DATA` carries no price**, so no deal value can be computed;
  those rows are stored but do not alert on value.

---

## 6. Response shapes (phase 2)

### `live-analysis-volume-gainers` (volume spurts)

```json
{"symbol": "SPAL", "companyName": "S. P. Apparels Limited",
 "volume": 1289999, "week1AvgVolume": 186676,
 "week1volChange": 6.910332562244378, "week2AvgVolume": 134587,
 "week2volChange": 9.584818958908182, "ltp": 1021, "pChange": 7.14,
 "turnover": 8016.11985}
```

**`week1volChange` is a multiple (6.91x), not a percent.** It equals
`volume / week1AvgVolume` to 5 significant figures. Reading it as a percent
would make a 7x spurt look like noise.

### `live-analysis-oi-spurts-underlyings` (OI spurts)

```json
{"symbol": "ATHERENERG", "latestOI": 15621, "prevOI": 5590,
 "changeInOI": 10031, "avgInOI": 179.45, "volume": 91084,
 "futValue": 51322.81567, "optValue": 51207140043, "total": 69528.14111,
 "premValue": 18205.32543, "underlyingValue": 1616}
```

**`avgInOI` is not an average — it is the percent change in OI.**
`(15621-5590)/5590*100 = 179.45`, matching the field exactly. The fetcher
renames it `oi_change_pct`; nothing downstream is allowed to see a field
called `avg_oi`. `optValue` is a different unit again (rupee notional, not
lakh premium) — see the unit table below.

### `snapshot-derivatives-equity` (most active contracts)

```json
{"volume": {"data": [{"identifier": "OPTIDXNIFTY01-09-2026PE24100.00",
  "numberOfContractsTraded": 7597655, "totalTurnover": 282283.27387,
  "premiumTurnover": 119299548.84887, "openInterest": 152558,
  "lastPrice": 35.25, "pChange": -43.28}]}, "value": {"data": [...]}}
```

**`totalTurnover` / `premiumTurnover` units could not be verified.**
`premiumTurnover / totalTurnover` ranged from 253x to 763x across five
contracts on the same poll — no single unit conversion reconciles that
spread, and neither `contracts × lastPrice` nor `contracts × strike`
lands on either field under any tested unit. The processor for this
category therefore uses only `pChange` and `openInterest`, and treats
`totalTurnover` purely as NSE's own top-N ranking, never as an amount.

### `option-chain-v3` (option chain)

Returned `{}` on every attempt so far (§4 footnote) — the shape below is
from NSE's documented v3 response, not a captured sample:

```json
{"records": {"timestamp": "...", "underlying": "NIFTY",
  "data": [{"strikePrice": 24100,
    "CE": {"openInterest": 152558, "totalTradedVolume": ..., "underlyingValue": 24175.65},
    "PE": {"openInterest": 98211, "totalTradedVolume": ..., "underlyingValue": 24175.65}}]}}
```

Because the OI unit is unverified, the rule is built entirely on **PCR**
(total PE OI / total CE OI) — a ratio of two same-endpoint, same-unit
fields, so it stays correct whatever that unit turns out to be.

### `fiidiiTradeReact` (FII/DII)

```json
[{"category": "DII", "buyValue": "16539.38", "sellValue": "11355.45",
  "netValue": "5183.93", "date": "28-Aug-2026"},
 {"category": "FII/FPI", "buyValue": "13263.62", "sellValue": "18303.42",
  "netValue": "-5039.8", "date": "28-Aug-2026"}]
```

**A bare list, not `{"data": [...]}`** — the only endpoint in the project
shaped that way. Values are strings, already in crore (see unit table).

### `market-data-pre-open` (pre-open)

```json
{"advances": 1244, "declines": 542, "unchanged": 286,
 "data": [{"metadata": {"symbol": "LANCORHOL", "lastPrice": 33.00,
   "pChange": 8.19, "finalQuantity": 202763, "totalTurnover": 6691179,
   "iep": 33.00}, "detail": {"preOpenMarket": {...}}}]}
```

Rows are nested one level deeper than every other feed
(`data[].metadata`, `data[].detail`). At 2072 rows / 2 MB this is the
heaviest feed in the project — only rows moving at least
`rules.pre_open.persist_min_pct_move` (default 2.0%) are written to the
observation table; the full payload is still captured in the raw
snapshot regardless.

---

## 7. Unit conventions

**NSE is not consistent about money and quantity units between endpoints,
even when two fields have the same name or represent the same concept.**
Every row below was confirmed by arithmetic against a live sample —
`quantity × price` reproducing the reported turnover, or one field
reconciling exactly against another — not assumed from the field name or
copied from a similar-looking endpoint. `bbnse/fetchers/base.py` has one
converter per unit (`lakh_to_cr`, `rupees_to_cr`, `cr_to_cr`,
`lakh_shares_to_shares`) so every fetcher states its conversion explicitly
at the call site.

| Endpoint | Field | Unit | How it was confirmed |
|---|---|---|---|
| gainers / losers | `turnover` | **lakh** | TCS: 4,054,069 sh × ₹2,342 ≈ ₹94,946 lakh vs reported 94,377.51 |
| volume spurts | `turnover` | **lakh** | median(volume×ltp / turnover) = 100,960 across 25 rows |
| volume spurts | `week1volChange`, `week2volChange` | **multiple**, not % | equals `volume / weekNAvgVolume` to 5 s.f. |
| price band hitters | `turnover` | **crore** | MASTEK 54.49 lakh sh × turnover 1002.87 implies avg price 1840 vs LTP 1934 (ratio 0.95) |
| price band hitters | `totalTradedVol` | **lakh shares** | same cross-check as above |
| most active (value) | `totalTradedValue` | **rupees** | median(qty×lastPrice / value) = 1.01 across 20 rows — the highest-risk field in the project, one keystroke from `lakh_to_cr` |
| advance/decline | `totalTradedValue` (per row) | **crore** | TEJASNET 431.6 (lakh) sh vs 2418 (crore) implies avg price ≈560 vs LTP 549 |
| advance/decline | `totalTradedVolume` (per row) | **lakh shares** | same cross-check |
| ETF | `trdVal` | **rupees** | SILVERBEES 26,759,523 sh × 230.26 / trdVal = 1.01 |
| pre-open (CM/FO) | `totalTurnover` | **rupees** | `finalQuantity × lastPrice` reproduced it **exactly** (ratio 1.000) on every row checked, e.g. LANCORHOL 202,763×33.00 = 6,691,179 |
| OI spurts | `futValue`, `premValue`, `total` | **lakh** | `total == futValue + premValue` held on all 20 rows checked |
| OI spurts | `avgInOI` | **percent change**, not average | `(latestOI-prevOI)/prevOI×100` matches exactly — see §6 |
| OI spurts | `optValue` | **rupees** (option notional) | median `optValue/premValue` ≈ 5.8e6 — far too large to be a unit scaling of the same quantity |
| derivatives watch | `totalTurnover`, `value` | **rupees** | `volume × lastPrice / totalTurnover` = 1.001–1.002 across all rows; `value == totalTurnover` exactly |
| most active contracts | `totalTurnover`, `premiumTurnover` | **UNVERIFIED** | no consistent ratio survives across rows (253x–763x spread) — rule uses `pChange`/`openInterest` only, see §6 |
| option chain | `openInterest` | **UNVERIFIED** | endpoint has returned `{}` on every attempt so far — rule uses PCR (a same-endpoint ratio) instead of absolute OI, see §6 |
| FII/DII | `buyValue`, `sellValue`, `netValue` | **crore** (already target unit) | `buy - sell == net` exactly; magnitude (thousands) is only sensible as crore for a single session |
| all indices | `last`, `variation`, `open/high/low` | **index points** | no money field on this endpoint, no ambiguity |
| SLB | `spreadPer` | **percent** (dimensionless) | `spread / underLyingLtp × 100` matches to the payload's own rounding |
| SLB | `turnOver`, `transactionValue` | **UNVERIFIED** | every row at verification time had zero volume — nothing to cross-check; not used by the rule, see §8 |
| Closing Auction Session | `finalValue`, `totalValue` | **rupees** (verified by reading code, not arithmetic) | the frontend's own divisor map (`{Lakhs:1e5, Crores:1e7, Billions:1e9}`) is applied to the raw value, which only makes sense if the raw value is rupees; no live numeric row was available to cross-check, see §8 |
| GSM / ASM / surveillance price bands | *(none — all fields are categorical or dates)* | **n/a** | the one set of endpoints in this project with no money unit at all |

Six distinct conventions across eleven endpoints for what is nominally the
same concept ("value traded"): lakh, crore, rupees, lakh-shares,
index-points, and — for two derivatives fields — genuinely unverified.
Reusing a converter across endpoints without checking this table first is
how a threshold silently goes 100x–10,000,000x wrong.

---

## 8. Phase 3 — resolved 2026-09-01

The three categories below were originally marked unresolved: their landing
pages had zero literal `.js`-suffixed script tags carrying real logic, so an
early Tier-3 scan (regex-matching `src="...js"`) concluded they were legacy
Drupal pages with no client-side data loading at all.

**That conclusion was wrong, and the bug was in the discovery script, not the
site.** Every real script tag on these pages carries a cache-busting query
string (`src="/dist/js/.../foo.js?v=28082026"`), which a regex anchored on
`.js` as the literal string *end* silently skips. Widening the match to any
`src="..."` and then filtering out the ~25 shared bundles (jquery,
bootstrap, the nav, moment.js, etc.) surfaces exactly one page-specific
bundle per page:

| Page | Page-specific bundle |
|---|---|
| `/market-data/price-bands-surveillance-actions` | *(none — this page is a link hub, see below)* |
| `/reports/gsm` | `/dist/js/sections/reports/gsm.js` |
| `/reports/asm` | `/dist/js/sections/reports/asm.js` |
| `/reports/price-band-changes` | `/dist/js/sections/reports/price-band-changes.js` |
| `/market-data/securities-lending-and-borrowing` | `/dist/js/sections/live-market/liveMarketSLB.js` |
| `/market-data/closing-auction-session` | Next.js `/_next/static/chunks/*.js` (this page has been migrated to the new gateway since the original scan) |

**"Price Bands & Surveillance Actions" turned out to be a hub page, not a
data page** — it links to three separate live reports rather than rendering
one itself. Those three reports cover the category between them:

| Sub-category | Endpoint | Referer | Rows (2026-09-01) |
|---|---|---|---|
| GSM (Graded Surveillance Measure) | `/api/reportGSM` | `/reports/gsm` | 82 |
| ASM (Additional Surveillance Measure) | `/api/reportASM` | `/reports/asm` | 144 longterm + 90 shortterm |
| Price band changes | `/api/eqsurvactions` | `/reports/price-band-changes` | 6 |

Securities Lending & Borrowing:

| Endpoint | Referer | Notes |
|---|---|---|
| `/api/live-analysis-slb-series-master` | `/market-data/securities-lending-and-borrowing` | Metadata only — its `filter.series.key` is the current month's series code |
| `/api/live-analysis-slb?series=<key>` | same | 23 rows. **`series` rolls over monthly** — re-derive it from series-master every poll, never hardcode it (see `fetchers/slb.py`) |
| `/api/price-watch-slb?series=<key>` | same | Aggregate open-position summary; empty at verification time |

Closing Auction Session:

| Endpoint | Referer |
|---|---|
| `/api/NextApi/apiClient/casApi?functionName=getCASData` | `/market-data/closing-auction-session` |

CAS returned `data: []` at verification time (a Tuesday mid-session, outside
the narrow pre-close auction window) both times it was checked — the field
shape (`symbol`, `refrencePrice`, `lowerBand`/`upperBand`, `finalPrice`,
`finalValue`, `iiqAtEP`, ...) comes from the Next.js bundle's own column
definitions, and the `finalValue` unit (rupees) was confirmed by reading the
bundle's own display-divisor logic (`{Lakhs:1e5, Crores:1e7,
Billions:1e9}[unit]` applied to the raw value) rather than by arithmetic
against a live number — re-confirm once a populated response is observed.

All GSM/ASM/price-band-change fields are categorical or date-valued —
symbol, stage, description, effective date — so unlike almost everything
else in this project, there was no money unit to get wrong. SLB's
`spreadPer` was verified as a plain percent
(`spread / underLyingLtp × 100`); its `turnOver` / `transactionValue`
remain unverified (every live row had zero volume) and are carried through
unused, the same pattern as `most_active_contracts` turnover in §7.
