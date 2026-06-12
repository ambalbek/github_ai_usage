"""Prometheus exporter for GitHub Copilot AI Credits / Premium Request usage.

Multi-entity: iterates over one or more organizations and/or enterprises.
For each entity it prefers the premium_request endpoint (which carries the
per-model breakdown). If that returns no items it falls back to the general
billing usage endpoint, filtered to AI-usage SKUs (seat licenses excluded).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

import requests
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("copilot_premium_exporter")

USAGE_METRICS: dict[str, str] = {
    "github_premium_request_usage_gross_quantity": "grossQuantity",
    "github_premium_request_usage_net_quantity": "netQuantity",
    "github_premium_request_usage_discount_quantity": "discountQuantity",
    "github_premium_request_usage_gross_amount": "grossAmount",
    "github_premium_request_usage_net_amount": "netAmount",
    "github_premium_request_usage_discount_amount": "discountAmount",
    "github_premium_request_usage_price_per_unit": "pricePerUnit",
}

# General endpoint uses "quantity" where premium_request splits gross/net.
GENERAL_USAGE_FIELD_MAP: dict[str, str] = {
    "github_premium_request_usage_gross_quantity": "quantity",
    "github_premium_request_usage_net_quantity": "quantity",
    "github_premium_request_usage_gross_amount": "grossAmount",
    "github_premium_request_usage_net_amount": "netAmount",
    "github_premium_request_usage_discount_amount": "discountAmount",
    "github_premium_request_usage_price_per_unit": "pricePerUnit",
}

LABEL_NAMES = ["type", "name", "product", "sku", "model", "unit", "year", "month"]
CONFIG_PATH = Path(__file__).parent / "config.json"

# Seat-license SKUs are subscriptions, NOT AI usage. Excluded from the general
# fallback so they don't inflate spend totals. Compared case-insensitively.
DEFAULT_EXCLUDE_SKUS = {"copilot business", "copilot enterprise"}


def _as_list(value: Any) -> list[str]:
    """Coerce a config value (list, comma string, or scalar) into a clean list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


@dataclass
class ExporterConfig:
    token: str
    organizations: list[str] = field(default_factory=list)
    enterprises: list[str] = field(default_factory=list)
    exclude_skus: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDE_SKUS))
    cache_ttl: int = 900
    http_timeout: int = 30
    port: int = 9185
    host: str = "0.0.0.0"
    log_level: str = "INFO"
    api_version: str = "2022-11-28"
    api_base: str = "https://api.github.com"

    @classmethod
    def load(cls, config_path: Path = CONFIG_PATH) -> "ExporterConfig":
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            logger.critical("GITHUB_TOKEN environment variable is required")
            raise SystemExit(1)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.critical("Failed to read %s: %s", config_path, exc)
            raise SystemExit(1) from exc

        # Accept plural arrays, legacy singular keys, and env overrides.
        orgs = _as_list(raw.get("github_organizations")) or _as_list(raw.get("github_organization"))
        ents = _as_list(raw.get("github_enterprises")) or _as_list(raw.get("github_enterprise"))
        orgs = _as_list(os.environ.get("GITHUB_ORGS")) or orgs
        ents = _as_list(os.environ.get("GITHUB_ENTERPRISES")) or ents

        if not orgs and not ents:
            logger.critical(
                "Configure at least one of github_organizations / github_enterprises"
            )
            raise SystemExit(1)

        exclude = raw.get("exclude_skus")
        exclude_set = ({s.lower() for s in _as_list(exclude)}
                       if exclude is not None else set(DEFAULT_EXCLUDE_SKUS))

        return cls(
            token=token,
            organizations=orgs,
            enterprises=ents,
            exclude_skus=exclude_set,
            cache_ttl=raw.get("cache_ttl_seconds", 900),
            http_timeout=raw.get("http_timeout_seconds", 30),
            port=raw.get("exporter_port", 9185),
            host=raw.get("exporter_host", "0.0.0.0"),
            log_level=raw.get("log_level", "INFO"),
            api_version=raw.get("api_version", "2022-11-28"),
            api_base=raw.get("api_base", "https://api.github.com").rstrip("/"),
        )

    def entities(self) -> list[tuple[str, str]]:
        """Return (type, name) pairs. type is 'organization' or 'enterprise'
        to match the dashboard's `type` label values."""
        return ([("enterprise", e) for e in self.enterprises]
                + [("organization", o) for o in self.organizations])


@dataclass
class CacheEntry:
    items: list[dict[str, Any]]   # each tagged with _type/_name/_year/_month
    expires_at: float


class CopilotPremiumCollector:
    """Custom Prometheus collector for GitHub Copilot billing data."""

    def __init__(self, config: ExporterConfig, registry: CollectorRegistry | None = REGISTRY):
        self._config = config
        self._session = self._build_session()
        self._cache: CacheEntry | None = None
        self._cache_lock = Lock()
        self._last_success_ts = 0.0
        self._last_fetch_ok = 0

        self._scrape_duration = Histogram(
            "github_premium_request_scrape_duration_seconds",
            "Duration of a real GitHub billing API fetch (cache hits excluded)",
            buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0),
            registry=registry,
        )
        self._scrape_failures = Counter(
            "github_premium_request_scrape_failures_total",
            "Total number of failed GitHub billing API fetches",
            registry=registry,
        )
        if registry is not None:
            registry.register(self)

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._config.token}",
            "X-GitHub-Api-Version": self._config.api_version,
            "User-Agent": "copilot-premium-exporter/2.0",
        })
        retry = Retry(total=3, backoff_factor=1,
                      status_forcelist=[500, 502, 503, 504], allowed_methods=["GET"])
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _endpoint(self, etype: str, name: str, kind: str) -> str:
        scope = "enterprises" if etype == "enterprise" else "organizations"
        suffix = "premium_request/usage" if kind == "premium_request" else "usage"
        return f"{self._config.api_base}/{scope}/{name}/settings/billing/{suffix}"

    def _get(self, url: str) -> dict[str, Any] | None:
        """GET a billing URL; return parsed JSON on 200, else None.

        Times only the real HTTP call so the duration histogram reflects true
        API latency, not cache hits (this method runs solely on cache miss).
        """
        fetch_start = time.monotonic()
        try:
            resp = self._session.get(url, timeout=self._config.http_timeout)
        except requests.RequestException as exc:
            self._scrape_duration.observe(time.monotonic() - fetch_start)
            logger.error("HTTP error on %s: %s", url, exc)
            self._scrape_failures.inc()
            return None

        self._scrape_duration.observe(time.monotonic() - fetch_start)

        if resp.status_code == 200:
            return resp.json()

        self._scrape_failures.inc()
        body = resp.text[:300]
        if resp.status_code in (401, 403):
            logger.error(
                "Auth failed (%d) for %s — token needs manage_billing:enterprise "
                "(classic) or Billing:Read (fine-grained). Body: %s",
                resp.status_code, url, body)
        elif resp.status_code == 404:
            logger.error(
                "404 for %s — wrong scope (org vs enterprise slug) or entity not "
                "on Enhanced Billing Platform. Body: %s", url, body)
        elif resp.status_code == 429 or resp.headers.get("X-RateLimit-Remaining") == "0":
            logger.warning("Rate limited; resets at %s. Serving cached data.",
                           resp.headers.get("X-RateLimit-Reset", "unknown"))
        else:
            logger.error("HTTP %d for %s: %s", resp.status_code, url, body)
        return None

    def _tag(self, items: list[dict[str, Any]], etype: str, name: str,
             year: str, month: str) -> list[dict[str, Any]]:
        out = []
        for it in items:
            tagged = dict(it)
            tagged["_type"] = etype
            tagged["_name"] = name
            tagged["_year"] = year
            tagged["_month"] = month
            # Normalize a missing or JSON-null model to "" so it renders as an
            # empty label rather than the literal string "None".
            if tagged.get("model") is None:
                tagged["model"] = ""
            out.append(tagged)
        return out

    def _fetch_entity(self, etype: str, name: str) -> tuple[list[dict[str, Any]], bool]:
        """Fetch one entity. Returns (tagged_items, http_ok).

        http_ok is True if at least one endpoint responded 200 (even if empty),
        so a genuinely-zero-usage period is not treated as a failure.
        """
        now = datetime.now(timezone.utc)
        d_year, d_month = str(now.year), str(now.month)
        ok = False

        # Primary: premium_request (has the model field; no seat licenses).
        data = self._get(self._endpoint(etype, name, "premium_request"))
        if data is not None:
            ok = True
            tp = data.get("timePeriod", {}) or {}
            year = str(tp.get("year", d_year))
            month = str(tp.get("month", d_month))
            items = data.get("usageItems", []) or []
            if items:
                logger.info("%s/%s premium_request -> %d items", etype, name, len(items))
                return self._tag(items, etype, name, year, month), ok

        # Fallback: general usage, filtered to AI SKUs (drop seat licenses).
        logger.info("%s/%s premium_request empty -> general usage fallback", etype, name)
        data = self._get(self._endpoint(etype, name, "usage"))
        if data is not None:
            ok = True
            all_items = data.get("usageItems", []) or []
            filtered = [
                it for it in all_items
                if (it.get("product") or "").lower() == "copilot"
                and (it.get("sku") or "").strip().lower() not in self._config.exclude_skus
            ]
            dropped = len(all_items) - len(filtered)
            logger.info("%s/%s general -> %d Copilot AI items (%d seat-license rows excluded)",
                        etype, name, len(filtered), dropped)
            return self._tag(filtered, etype, name, d_year, d_month), ok

        return [], ok

    def _fetch(self) -> CacheEntry:
        now = time.monotonic()
        with self._cache_lock:
            if self._cache and self._cache.expires_at > now:
                return self._cache

        all_items: list[dict[str, Any]] = []
        any_ok = False
        for etype, name in self._config.entities():
            items, ok = self._fetch_entity(etype, name)
            all_items.extend(items)
            any_ok = any_ok or ok

        self._last_fetch_ok = 1 if any_ok else 0
        if any_ok:
            self._last_success_ts = time.time()

        entry = CacheEntry(items=all_items, expires_at=time.monotonic() + self._config.cache_ttl)
        with self._cache_lock:
            self._cache = entry
        return entry

    def collect(self) -> Iterable[GaugeMetricFamily]:
        entry = self._fetch()  # duration observed inside _get on real HTTP only

        families = {name: GaugeMetricFamily(name, USAGE_METRICS[name], labels=LABEL_NAMES)
                    for name in USAGE_METRICS}

        for item in entry.items:
            label_values = [
                item.get("_type", ""),
                item.get("_name", ""),
                str(item.get("product", "")),
                str(item.get("sku", "")),
                str(item.get("model", "")),
                str(item.get("unitType", "")),
                item.get("_year", ""),
                item.get("_month", ""),
            ]
            for metric_name, json_key in USAGE_METRICS.items():
                value = item.get(json_key)
                if value is None:
                    fb = GENERAL_USAGE_FIELD_MAP.get(metric_name)
                    value = item.get(fb, 0) if fb else 0
                families[metric_name].add_metric(label_values, float(value or 0))

        yield from families.values()

        success = GaugeMetricFamily(
            "github_premium_request_scrape_success",
            "1 if the last refresh reached GitHub for at least one entity, else 0")
        success.add_metric([], float(self._last_fetch_ok))
        yield success

        last = GaugeMetricFamily(
            "github_premium_request_last_scrape_timestamp_seconds",
            "Unix timestamp of the last SUCCESSFUL GitHub fetch (0 if never)")
        last.add_metric([], self._last_success_ts)
        yield last


def main() -> None:
    config = ExporterConfig.load()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("Starting exporter on %s:%d | orgs=%s enterprises=%s | cache_ttl=%ds",
                config.host, config.port, config.organizations, config.enterprises,
                config.cache_ttl)
    CopilotPremiumCollector(config)
    start_http_server(config.port, addr=config.host)
    logger.info("Exporter ready — metrics on /metrics")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()