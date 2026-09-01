"""NSE HTTP transport.

Everything in here exists because of something NSE actually does:

* Bare `requests` gets 403 at the Akamai edge in ~90ms. A full browser header
  set gets 200 plus the bot cookies _abck / ak_bmsc / bm_sz / AKA_A2 -- but
  only sometimes: the homepage still 403s under `requests` because urllib3's
  TLS fingerprint (JA3) is not Chrome's. The API calls happened to survive
  that, which is luck, not design.
* `curl_cffi` with Chrome impersonation matches the TLS fingerprint and gets
  a clean 200 on the homepage. That is the default backend.
* Cookies age out, so the session self-rebootstraps on 401/403 and after
  `session_max_age_sec`.
* API calls need a Referer matching the page that would normally issue them,
  plus XHR-ish Sec-Fetch-Site: same-origin.
* NSE runs two API generations at once (legacy /api/... and the newer
  /api/NextApi/... gateway). Both are driven from here.

Backend is selected by `http.backend` in config.yaml. Both backends expose the
same interface to the rest of the codebase.

A note on header ownership: when impersonating, curl_cffi supplies the browser
identity headers (User-Agent, sec-ch-ua, Accept-Language, Accept-Encoding) to
match the TLS fingerprint it presents. Overriding them with our own hardcoded
Chrome/124 strings would produce a *worse* fingerprint than not impersonating
at all -- TLS claiming one browser while headers claim another is exactly the
mismatch bot detection looks for. So identity headers are backend-owned;
we only ever set request-semantic headers.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

from .logging_setup import get_logger

log = get_logger(__name__)

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.exceptions import (
        RequestException as CurlRequestException,
    )
    CURL_CFFI_AVAILABLE = True
except ImportError:                                   # pragma: no cover
    curl_requests = None
    CurlRequestException = None
    CURL_CFFI_AVAILABLE = False

# Every network failure either backend can raise.
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    (requests.RequestException,)
    + ((CurlRequestException,) if CURL_CFFI_AVAILABLE else ())
)

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Browser identity. Only sent on the `requests` backend -- under curl_cffi
# these come from the impersonation profile so TLS and headers agree.
_IDENTITY_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# What kind of request this is. Always ours, on both backends.
_DOC_SEMANTICS = {
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_API_SEMANTICS = {
    "Accept": "*/*",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


class NSEBlockedError(RuntimeError):
    """Raised when NSE keeps refusing us after every retry is spent."""


class NSEFetchError(RuntimeError):
    """Non-recoverable fetch failure for one endpoint."""


@dataclass
class FetchResult:
    url: str
    status: int
    json: Any = None
    text: str = ""
    content: bytes = b""
    elapsed_sec: float = 0.0
    attempts: int = 1
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class NSESession:
    """Thread-safe, self-healing NSE session."""

    def __init__(self, cfg):
        self.cfg = cfg
        h = cfg.section("http")

        self.backend = self._resolve_backend(h.get("backend", "curl_cffi"))
        # "chrome" is a rolling alias for the newest profile curl_cffi ships,
        # which is what we want -- pinning goes stale and stale is detectable.
        self.impersonate = h.get("impersonate", "chrome")

        self.host = cfg.get("endpoints_host", "https://www.nseindia.com")
        self.bootstrap_url = h.get("bootstrap_url", "https://www.nseindia.com/")
        self.session_max_age = float(h.get("session_max_age_sec", 600))
        self.connect_timeout = float(h.get("connect_timeout", 10))
        self.read_timeout = float(h.get("read_timeout", 25))
        jitter = h.get("jitter_sec", [0.4, 1.2])
        self.jitter_lo, self.jitter_hi = float(jitter[0]), float(jitter[1])

        r = h.get("retry", {}) or {}
        self.max_attempts = int(r.get("max_attempts", 4))
        self.backoff_base = float(r.get("backoff_base_sec", 2.0))
        self.backoff_max = float(r.get("backoff_max_sec", 60))
        self.rebootstrap_on = set(r.get("rebootstrap_on", [401, 403]))
        self.retry_on = set(r.get("retry_on", [408, 429, 500, 502, 503, 504]))

        self._lock = threading.RLock()
        self._session: Any = None
        self._bootstrapped_at: float = 0.0
        self._warmed: set[str] = set()
        # Observability for the request-load budget (see README).
        self._request_count = 0

    @staticmethod
    def _resolve_backend(requested: str) -> str:
        requested = (requested or "curl_cffi").strip().lower()
        if requested == "curl_cffi" and not CURL_CFFI_AVAILABLE:
            log.warning(
                "http.backend is curl_cffi but the package is not installed; "
                "falling back to requests (pip install curl-cffi). Expect 403s "
                "on the homepage bootstrap."
            )
            return "requests"
        if requested not in {"requests", "curl_cffi"}:
            log.warning("unknown http.backend, using curl_cffi",
                        extra={"requested": requested})
            return "curl_cffi" if CURL_CFFI_AVAILABLE else "requests"
        return requested

    @property
    def uses_impersonation(self) -> bool:
        return self.backend == "curl_cffi"

    @property
    def request_count(self) -> int:
        return self._request_count

    # -- header assembly -----------------------------------------------------
    def _headers(self, semantics: dict, referer: str | None = None) -> dict:
        out = {} if self.uses_impersonation else dict(_IDENTITY_HEADERS)
        out.update(semantics)
        if referer is not None:
            out["Referer"] = urljoin(self.host, referer or "/")
        return out

    # -- session lifecycle ---------------------------------------------------
    def _new_session(self):
        if self.uses_impersonation:
            # Impersonation owns the identity headers; do not fight it.
            return curl_requests.Session(impersonate=self.impersonate)
        s = requests.Session()
        s.headers.update(_IDENTITY_HEADERS)
        return s

    def bootstrap(self, force: bool = False) -> None:
        with self._lock:
            fresh = (time.time() - self._bootstrapped_at) < self.session_max_age
            if self._session is not None and fresh and not force:
                return

            self._session = self._new_session()
            self._warmed.clear()
            try:
                self._request_count += 1
                resp = self._session.get(
                    self.bootstrap_url,
                    headers=self._headers(_DOC_SEMANTICS),
                    timeout=(self.connect_timeout, self.read_timeout),
                )
                cookies = list(self._session.cookies.keys())
                self._bootstrapped_at = time.time()
                log.info("session bootstrapped",
                         extra={"status": resp.status_code, "cookies": cookies,
                                "backend": self.backend,
                                "impersonate": self.impersonate
                                if self.uses_impersonation else None})
                if resp.status_code == 403:
                    # Under curl_cffi this should not happen. Under requests it
                    # is expected, and usable cookies are often set regardless.
                    log.warning(
                        "bootstrap returned 403 but cookies were set; "
                        "continuing"
                        + ("" if self.uses_impersonation else
                           " (set http.backend: curl_cffi to fix properly)"),
                        extra={"cookies": cookies, "backend": self.backend},
                    )
            except NETWORK_ERRORS as exc:
                self._session = None
                raise NSEBlockedError(f"bootstrap failed: {exc}") from exc

    def warm(self, page_path: str) -> None:
        """Fetch a landing page once so its Referer looks legitimate."""
        if not page_path or page_path in self._warmed:
            return
        self.bootstrap()
        url = urljoin(self.host, page_path)
        try:
            self._sleep_jitter()
            self._request_count += 1
            self._session.get(url, headers=self._headers(_DOC_SEMANTICS),
                              timeout=(self.connect_timeout, self.read_timeout))
            self._warmed.add(page_path)
            log.debug("warmed landing page", extra={"page": page_path})
        except NETWORK_ERRORS as exc:
            log.debug("warm failed (non-fatal)",
                      extra={"page": page_path, "err": str(exc)})

    # -- helpers -------------------------------------------------------------
    def _sleep_jitter(self) -> None:
        time.sleep(random.uniform(self.jitter_lo, self.jitter_hi))

    def _backoff(self, attempt: int) -> float:
        delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)
        return delay * random.uniform(0.7, 1.3)   # decorrelate parallel jobs

    # -- main entry point ----------------------------------------------------
    def get_json(self, url: str, referer: str = "/", *,
                 params: dict | None = None,
                 timeout: float | None = None) -> FetchResult:
        """GET a JSON endpoint with retry, backoff and session repair."""
        self.bootstrap()
        started = time.time()
        last_status = 0
        last_err = ""

        for attempt in range(1, self.max_attempts + 1):
            self._sleep_jitter()
            try:
                self._request_count += 1
                resp = self._session.get(
                    url,
                    headers=self._headers(_API_SEMANTICS, referer),
                    params=params,
                    timeout=(self.connect_timeout, timeout or self.read_timeout),
                )
                last_status = resp.status_code

                if resp.status_code in self.rebootstrap_on:
                    log.warning("blocked, re-bootstrapping session",
                                extra={"url": url, "status": resp.status_code,
                                       "attempt": attempt})
                    if attempt < self.max_attempts:
                        time.sleep(self._backoff(attempt))
                        self.bootstrap(force=True)
                        self.warm(referer)
                        continue

                elif resp.status_code in self.retry_on:
                    log.warning("retryable status",
                                extra={"url": url, "status": resp.status_code,
                                       "attempt": attempt})
                    if attempt < self.max_attempts:
                        time.sleep(self._backoff(attempt))
                        continue

                elif resp.ok:
                    payload = None
                    ctype = resp.headers.get("Content-Type", "")
                    if "json" in ctype:
                        try:
                            payload = resp.json()
                        except ValueError as exc:
                            # A 200 that is not JSON usually means an
                            # interstitial/bot page rather than real data.
                            last_err = f"json decode: {exc}"
                            log.warning("200 but undecodable JSON",
                                        extra={"url": url, "attempt": attempt,
                                               "ctype": ctype})
                            if attempt < self.max_attempts:
                                time.sleep(self._backoff(attempt))
                                self.bootstrap(force=True)
                                continue
                    return FetchResult(
                        url=url, status=resp.status_code, json=payload,
                        text=resp.text if payload is None else "",
                        content=resp.content,
                        elapsed_sec=time.time() - started,
                        attempts=attempt, headers=dict(resp.headers),
                    )
                else:
                    # 404 and friends: not worth retrying, surface immediately.
                    return FetchResult(
                        url=url, status=resp.status_code, content=resp.content,
                        text=resp.text, elapsed_sec=time.time() - started,
                        attempts=attempt, headers=dict(resp.headers),
                    )

            except NETWORK_ERRORS as exc:
                last_err = str(exc)
                log.warning("request exception",
                            extra={"url": url, "attempt": attempt,
                                   "err": last_err})
                if attempt < self.max_attempts:
                    time.sleep(self._backoff(attempt))
                    continue

        raise NSEFetchError(
            f"GET {url} failed after {self.max_attempts} attempts "
            f"(last status {last_status}, last error: {last_err or 'n/a'})"
        )

    def get_file(self, url: str, referer: str = "/") -> FetchResult:
        """Download a non-JSON archive file (CSV/zip) from nsearchives."""
        self.bootstrap()
        started = time.time()
        headers = self._headers({"Accept": "*/*",
                                 "Connection": "keep-alive"}, referer)
        for attempt in range(1, self.max_attempts + 1):
            self._sleep_jitter()
            try:
                self._request_count += 1
                resp = self._session.get(
                    url, headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
                if resp.ok:
                    return FetchResult(
                        url=url, status=resp.status_code, content=resp.content,
                        text=resp.text, elapsed_sec=time.time() - started,
                        attempts=attempt,
                    )
                if (resp.status_code in self.rebootstrap_on
                        and attempt < self.max_attempts):
                    time.sleep(self._backoff(attempt))
                    self.bootstrap(force=True)
                    continue
                if resp.status_code in self.retry_on and attempt < self.max_attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                return FetchResult(url=url, status=resp.status_code,
                                   content=resp.content, text=resp.text,
                                   elapsed_sec=time.time() - started,
                                   attempts=attempt)
            except NETWORK_ERRORS:
                if attempt < self.max_attempts:
                    time.sleep(self._backoff(attempt))
                    continue
                raise
        raise NSEFetchError(f"GET file {url} failed")


_SHARED: NSESession | None = None
_SHARED_LOCK = threading.Lock()


def get_session(cfg) -> NSESession:
    """One shared session process-wide: cookies are the scarce resource."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = NSESession(cfg)
        return _SHARED


def reset_session() -> None:
    """Drop the shared session. Used by tests."""
    global _SHARED
    with _SHARED_LOCK:
        _SHARED = None
