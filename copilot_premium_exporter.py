"""Prometheus exporter for GitHub Copilot AI Credits / Premium Request usage."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
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

# Fallback endpoint (general /settings/billing/usage) uses different keys.
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


@dataclass
class ExporterConfig:
    """Configuration loaded from config.json + GITHUB_TOKEN env var."""

    token: str
    enterprise: str = ""
    organization: str = ""
    cache_ttl: int = 900
    http_timeout: int = 30
    port: int = 9185
    host: str = "0.0.0.0"
    log_level: str = "INFO"
    api_version: str = "2022-11-28"
    api_base: str = "https://api.github.com"

    @classmethod
    def load(cls, config_path: Path = CONFIG_PATH) -> ExporterConfig:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            logger.critical("GITHUB_TOKEN environment variable is required")
            raise SystemExit(1)

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.critical("Failed to read %s: %s", config_path, exc)
            raise SystemExit(1) from exc

        enterprise = raw.get("github_enterprise", "").strip()
        organization = raw.get("github_organization", "").strip()
        if not enterprise and not organization:
            logger.critical(
                "Set either github_enterprise or github_organization in config.json"
            )
            raise SystemExit(1)

        return cls(
            token=token,
            enterprise=enterprise,
            organization=organization,
            cache_ttl=raw.get("cache_ttl_seconds", 900),
            http_timeout=raw.get("http_timeout_seconds", 30),
            port=raw.get("exporter_port", 9185),
            host=raw.get("exporter_host", "0.0.0.0"),
            log_level=raw.get("log_level", "INFO"),
            api_version=raw.get("api_version", "2022-11-28"),
            api_base=raw.get("api_base", "https://api.github.com").rstrip("/"),
        )

    @property
    def entity_type(self) -> str:
        return "enterprise" if self.enterprise else "organization"

    @property
    def entity_name(self) -> str:
        return self.enterprise or self.organization


@dataclass
class CacheEntry:
    items: list[dict[str, Any]]
    year: str
    month: str
    expires_at: float


class CopilotPremiumCollector:
    """Custom Prometheus collector for GitHub Copilot billing data."""

    def __init__(
        self,
        config: ExporterConfig,
        registry: CollectorRegistry | None = REGISTRY,
    ):
        self._config = config
        self._session = self._build_session()
        self._cache: CacheEntry | None = None
        self._cache_lock = Lock()

        self._scrape_duration = Histogram(
            "github_premium_request_scrape_duration_seconds",
            "Duration of premium request data collection",
            registry=registry,
        )
        self._scrape_failures = Counter(
            "github_premium_request_scrape_failures_total",
            "Total number of failed scrapes",
            registry=registry,
        )

        if registry is not None:
            registry.register(self)

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                # Fix: GitHub's documented Accept header is vnd.github+json.
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._config.token}",
                "X-GitHub-Api-Version": self._config.api_version,
                "User-Agent": "copilot-premium-exporter/1.0",
            }
        )
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _endpoint(self, kind: str) -> str:
        """Build the API URL. kind is 'premium_request' or 'general'."""
        base = self._config.api_base
        scope = "enterprises" if self._config.entity_type == "enterprise" else "organizations"
        name = self._config.entity_name
        if kind == "premium_request":
            return f"{base}/{scope}/{name}/settings/billing/premium_request/usage"
        return f"{base}/{scope}/{name}/settings/billing/usage"

    def _get(self, url: str) -> dict[str, Any] | None:
        """GET a URL; return parsed JSON on 200, None on any failure.

        No query params — the endpoint is undocumented for year/month and
        passing them empirically causes empty responses.
        """
        try:
            resp = self._session.get(url, timeout=self._config.http_timeout)
        except requests.RequestException as exc:
            logger.error("HTTP error on %s: %s", url, exc)
            self._scrape_failures.inc()
            return None

        if resp.status_code == 200:
            return resp.json()

        self._scrape_failures.inc()
        body = resp.text[:300]
        if resp.status_code in (401, 403):
            logger.error(
                "Auth failed (%d) for %s — token needs "
                "manage_billing:enterprise (classic) or Billing:Read (fine-grained). Body: %s",
                resp.status_code, url, body,
            )
        elif resp.status_code == 404:
            logger.error(
                "404 for %s — wrong scope (org vs enterprise slug) or "
                "entity not on Enhanced Billing Platform. Body: %s",
                url, body,
            )
        elif resp.status_code == 429 or resp.headers.get("X-RateLimit-Remaining") == "0":
            logger.warning(
                "Rate limited; resets at %s. Serving cached data.",
                resp.headers.get("X-RateLimit-Reset", "unknown"),
            )
        else:
            logger.error("HTTP %d for %s: %s", resp.status_code, url, body)
        return None

    def _fetch(self) -> CacheEntry | None:
        """Return cached or freshly-fetched usage. Tries premium_request first,
        falls back to general /settings/billing/usage if that returns nothing.
        Post-June-2026 AI Credits data may live in either endpoint depending
        on plan migration state.
        """
        now = time.monotonic()
        with self._cache_lock:
            if self._cache and self._cache.expires_at > now:
                return self._cache

        utc_now = datetime.now(timezone.utc)
        default_year = str(utc_now.year)
        default_month = str(utc_now.month)

        items: list[dict[str, Any]] = []
        year, month = default_year, default_month

        # Primary: premium_request endpoint (has the `model` field).
        data = self._get(self._endpoint("premium_request"))
        if data:
            tp = data.get("timePeriod", {}) or {}
            year = str(tp.get("year", default_year))
            month = str(tp.get("month", default_month))
            items = data.get("usageItems", []) or []
            logger.info(
                "premium_request endpoint returned %d items for %s/%s",
                len(items), self._config.entity_type, self._config.entity_name,
            )

        # Fallback: general usage endpoint (no `model` field, but at least gets Copilot SKU spend).
        if not items:
            logger.info("Falling back to general /settings/billing/usage endpoint")
            data = self._get(self._endpoint("general"))
            if data:
                all_items = data.get("usageItems", []) or []
                items = [
                    {**i, "model": ""}  # general endpoint has no model field
                    for i in all_items
                    if (i.get("product") or "").lower() == "copilot"
                ]
                logger.info("general endpoint returned %d Copilot items", len(items))

        if data is None and not items:
            return None  # complete failure — don't poison the cache

        entry = CacheEntry(
            items=items,
            year=year,
            month=month,
            expires_at=time.monotonic() + self._config.cache_ttl,
        )
        with self._cache_lock:
            self._cache = entry
        return entry

    def collect(self) -> Iterable[GaugeMetricFamily]:
        start = time.monotonic()
        entry = self._fetch()
        self._scrape_duration.observe(time.monotonic() - start)

        families = {
            name: GaugeMetricFamily(name, USAGE_METRICS[name], labels=LABEL_NAMES)
            for name in USAGE_METRICS
        }

        if entry is not None:
            for item in entry.items:
                # Premium-request schema has separate gross/net quantities;
                # general schema has only "quantity". Use a per-metric fallback.
                label_values = [
                    self._config.entity_type,
                    self._config.entity_name,
                    str(item.get("product", "")),
                    str(item.get("sku", "")),
                    str(item.get("model", "")),
                    str(item.get("unitType", "")),
                    entry.year,
                    entry.month,
                ]
                for metric_name, json_key in USAGE_METRICS.items():
                    value = item.get(json_key)
                    if value is None:
                        # General endpoint fallback for missing keys.
                        fallback_key = GENERAL_USAGE_FIELD_MAP.get(metric_name)
                        value = item.get(fallback_key, 0) if fallback_key else 0
                    families[metric_name].add_metric(label_values, float(value or 0))

        yield from families.values()

        success = GaugeMetricFamily(
            "github_premium_request_scrape_success",
            "1 if last scrape succeeded and returned any items, else 0",
        )
        success.add_metric([], 1.0 if entry and entry.items else 0.0)
        yield success

        last = GaugeMetricFamily(
            "github_premium_request_last_scrape_timestamp_seconds",
            "Unix timestamp of the last scrape attempt",
        )
        last.add_metric([], time.time())
        yield last


def main() -> None:
    config = ExporterConfig.load()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "Starting exporter on %s:%d (%s=%s, cache_ttl=%ds)",
        config.host, config.port, config.entity_type, config.entity_name, config.cache_ttl,
    )
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