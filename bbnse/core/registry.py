"""Endpoint registry.

Endpoints live in endpoints.yaml, not in Python. NSE migrates pages between
its two API generations without notice; when that happens you edit YAML and
re-run `python main.py verify-endpoints` rather than touching fetcher code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml

from .config import PROJECT_ROOT


@dataclass
class Endpoint:
    name: str
    generation: str                       # legacy | nextapi | archive
    path: str | None = None
    url: str | None = None                # archive entries carry an absolute URL
    params: dict[str, Any] = field(default_factory=dict)
    referer: str = "/"
    row_root: str | None = None
    extra_roots: list[str] = field(default_factory=list)
    buckets: list[str] = field(default_factory=list)
    status: str = "OK"                    # OK | UNRESOLVED
    page: str | None = None
    verified: dict[str, Any] = field(default_factory=dict)
    host: str = "https://www.nseindia.com"
    timeout: int = 25

    @property
    def resolved(self) -> bool:
        return self.status != "UNRESOLVED" and bool(self.path or self.url)

    @property
    def full_url(self) -> str:
        if self.url:
            return self.url
        if not self.path:
            raise ValueError(f"Endpoint '{self.name}' has no path or url")
        return urljoin(self.host, self.path)


class Registry:
    def __init__(self, endpoints: dict[str, Endpoint], defaults: dict):
        self._eps = endpoints
        self.defaults = defaults

    def __contains__(self, name: str) -> bool:
        return name in self._eps

    def __iter__(self):
        return iter(self._eps.values())

    def __len__(self) -> int:
        return len(self._eps)

    def get(self, name: str) -> Endpoint:
        if name not in self._eps:
            raise KeyError(
                f"Unknown endpoint '{name}'. Known: {sorted(self._eps)[:10]}..."
            )
        return self._eps[name]

    def names(self, *, resolved_only: bool = False) -> list[str]:
        return sorted(
            n for n, e in self._eps.items() if not resolved_only or e.resolved
        )

    def by_generation(self, generation: str) -> list[Endpoint]:
        return [e for e in self._eps.values() if e.generation == generation]


def extract_rows(payload: Any, root: str | None) -> list[dict]:
    """Pull a row list out of a payload using a dotted root path.

    Handles the shapes NSE actually uses: a bare list, a dict with a `data`
    key, and nested roots like `upper.AllSec.data` or `volume.data`.
    """
    if payload is None:
        return []
    if root is None:
        return payload if isinstance(payload, list) else []

    node: Any = payload
    for part in root.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return []
    if isinstance(node, list):
        return [r for r in node if isinstance(r, dict)]
    return []


def count_rows(payload: Any, ep: Endpoint) -> int:
    """Best-effort row count for drift detection.

    Handles the shapes NSE uses so `verify-endpoints` reports a real number
    for every endpoint: a bucketed root like "{bucket}.data" is summed across
    buckets, a dict root is counted by key, and extra_roots are included.
    Without this, bucketed feeds always read as 0 rows and a genuine outage
    would look identical to a healthy response.
    """
    if payload is None:
        return 0
    root = ep.row_root

    if root and "{bucket}" in root and ep.buckets:
        return sum(len(extract_rows(payload, root.format(bucket=b)))
                   for b in ep.buckets)

    total = len(extract_rows(payload, root))
    for extra in ep.extra_roots:
        total += len(extract_rows(payload, extra))
    if total:
        return total

    # Root resolves to a dict (holiday_master, market_status, advance_decline)
    # or the payload is a bare list.
    node: Any = payload
    if root:
        for part in root.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
    if isinstance(node, dict):
        return sum(len(v) if isinstance(v, list) else 1 for v in node.values())
    if isinstance(node, list):
        return len(node)
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return sum(len(v) if isinstance(v, list) else 1
                   for v in payload.values())
    return 0


@lru_cache(maxsize=2)
def load_registry(path: str | None = None) -> Registry:
    reg_path = Path(path) if path else PROJECT_ROOT / "endpoints.yaml"
    with reg_path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    defaults = doc.get("defaults", {}) or {}
    host = defaults.get("host", "https://www.nseindia.com")
    timeout = int(defaults.get("timeout", 25))

    eps: dict[str, Endpoint] = {}
    for name, raw in (doc.get("endpoints") or {}).items():
        raw = raw or {}
        eps[name] = Endpoint(
            name=name,
            generation=raw.get("generation", "legacy"),
            path=raw.get("path"),
            url=raw.get("url"),
            params={k: str(v) for k, v in (raw.get("params") or {}).items()},
            referer=raw.get("referer", "/"),
            row_root=raw.get("row_root"),
            extra_roots=list(raw.get("extra_roots") or []),
            buckets=list(raw.get("buckets") or []),
            status=raw.get("status", "OK"),
            page=raw.get("page"),
            verified=raw.get("verified") or {},
            host=host,
            timeout=timeout,
        )
    return Registry(eps, defaults)
