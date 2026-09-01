"""Alert universe.

Immediate alerts fire only for symbols in this set; reports always cover the
whole market. Constituent lists come from NSE's archive CSVs and are cached
locally so a network blip cannot silence alerting.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT
from .logging_setup import get_logger
from .registry import load_registry

log = get_logger(__name__)

CACHE = PROJECT_ROOT / "universe" / "universe.cache.json"

_SOURCES = {
    "nifty500": "universe_nifty500",
    "fo": "universe_fo",
    "all": "universe_equity_all",
}


def _symbols_from_csv(text: str, *candidates: str) -> set[str]:
    """Pull a symbol column out of an NSE CSV.

    NSE pads headers and values with spaces inconsistently across files
    (fo_mktlots.csv in particular), so everything gets stripped.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return set()
    norm = {(f or "").strip().upper(): f for f in reader.fieldnames}
    col = next((norm[c] for c in candidates if c in norm), None)
    if col is None:
        return set()
    out = set()
    for row in reader:
        val = (row.get(col) or "").strip().upper()
        # fo_mktlots has trailing note rows that are not symbols.
        if val and val.isascii() and " " not in val and len(val) <= 20:
            out.add(val)
    return out


class Universe:
    def __init__(self, cfg, session=None):
        self.cfg = cfg
        self.session = session
        u = cfg.section("universe")
        self.mode = u.get("mode", "fo_plus_nifty500")
        self.refresh_days = int(u.get("refresh_days", 7))
        self.always = {s.strip().upper() for s in (u.get("always_include") or [])}
        self.never = {s.strip().upper() for s in (u.get("never_include") or [])}
        self.watchlist_file = u.get("watchlist_file", "universe/watchlist.txt")
        self._symbols: set[str] | None = None

    # -- building ------------------------------------------------------------
    def _download(self, key: str) -> set[str]:
        ep = load_registry().get(_SOURCES[key])
        res = self.session.get_file(ep.full_url, referer="/")
        if not res.ok:
            raise RuntimeError(f"{key} download returned {res.status}")
        text = res.content.decode("utf-8", errors="replace")
        syms = _symbols_from_csv(text, "SYMBOL", "UNDERLYING")
        log.info("universe source downloaded",
                 extra={"source": key, "symbols": len(syms)})
        return syms

    def _load_cache(self) -> dict | None:
        if not CACHE.exists():
            return None
        try:
            doc = json.loads(CACHE.read_text(encoding="utf-8"))
            age = (datetime.now() - datetime.fromisoformat(doc["fetched_at"])).days
            if age > self.refresh_days:
                return None
            return doc
        except Exception:
            return None

    def _save_cache(self, parts: dict[str, list[str]]) -> None:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({
            "fetched_at": datetime.now().isoformat(), "parts": parts,
        }, indent=1), encoding="utf-8")

    def _watchlist(self) -> set[str]:
        path = Path(self.watchlist_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.exists():
            log.warning("watchlist file missing", extra={"path": str(path)})
            return set()
        out = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip().upper()
            if line:
                out.add(line)
        return out

    def build(self, force: bool = False) -> set[str]:
        if self.mode == "watchlist":
            syms = self._watchlist()
            return (syms | self.always) - self.never

        cached = None if force else self._load_cache()
        parts: dict[str, list[str]] = dict(cached["parts"]) if cached else {}

        needed = {"fo_plus_nifty500": ["fo", "nifty500"],
                  "nifty500": ["nifty500"],
                  "nifty200": ["nifty500"],
                  "all": ["all"]}.get(self.mode, ["fo", "nifty500"])

        missing = [k for k in needed if k not in parts]
        if missing:
            if self.session is None:
                raise RuntimeError(
                    "universe cache is cold and no session was provided"
                )
            for key in missing:
                try:
                    parts[key] = sorted(self._download(key))
                except Exception as exc:
                    log.error("universe source failed",
                              extra={"source": key, "err": str(exc)})
                    if cached and key in cached.get("parts", {}):
                        parts[key] = cached["parts"][key]
                    else:
                        parts[key] = []
            self._save_cache(parts)

        syms: set[str] = set()
        for key in needed:
            syms |= set(parts.get(key, []))
        return (syms | self.always) - self.never

    # -- queries -------------------------------------------------------------
    @property
    def symbols(self) -> set[str]:
        if self._symbols is None:
            try:
                self._symbols = self.build()
            except Exception as exc:
                log.error("universe build failed; alerting on nothing until "
                          "this is fixed", extra={"err": str(exc)})
                self._symbols = set(self.always)
        return self._symbols

    def __contains__(self, symbol: str) -> bool:
        # An empty universe means "not yet built"; fail open rather than
        # silently suppressing every alert.
        if not self.symbols:
            return True
        return (symbol or "").strip().upper() in self.symbols

    def __len__(self) -> int:
        return len(self.symbols)
