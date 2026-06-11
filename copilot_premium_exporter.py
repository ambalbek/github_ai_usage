"""Prometheus exporter for GitHub Copilot Premium Request usage (Enhanced Billing Platform)."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

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

# Metric names and their JSON keys in usageItems
USAGE_METRICS: dict[str, str] = {
    "github_premium_request_usage_gross_quantity": "grossQuantity",
    "github_premium_request_usage_net_quantity": "netQuantity",
    "github_premium_request_usage_discount_quantity": "discountQuantity",
    "github_premium_request_usage_gross_amount": "grossAmount",
    "github_premium_request_usage_net_amount": "netAmount",
    "github_premium_request_usage_discount_amount": "discountAmount",
    "github_premium_request_usage_price_per_unit": "pricePerUnit",
}

LABEL_NAMES = ["type", "name", "product", "sku", "model", "unit", "year"]

CONFIG_PATH = Path(__file__).parent / "config.json"


@dataclass
class ExporterConfig:
    """Configuration loaded from config.json + GITHUB_TOKEN env var."""

    token: str
    enterprise: str
    cache_ttl: int = 900
    http_timeout: int = 30
    port: int = 9185
    host: str = "0.0.0.0"
    log_level: str = "INFO"
    api_version: str = "2022-11-28"

    @classmethod
    def load(cls, config_path: Path = CONFIG_PATH) -> ExporterConfig:
        """Load config from config.json. Token comes from GITHUB_TOKEN env var."""
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            logger.critical("GITHUB_TOKEN environment variable is required")
            raise SystemExit(1)

        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.critical("Failed to read %s: %s", config_path, exc)
            raise SystemExit(1) from exc

        enterprise = raw.get("github_enterprise", "")
        if not enterprise:
            logger.critical("github_enterprise is required in config.json")
            raise SystemExit(1)

        return cls(
            token=token,
            enterprise=enterprise,
            cache_ttl=raw.get("cache_ttl_seconds", 900),
            http_timeout=raw.get("http_timeout_seconds", 30),
            port=raw.get("exporter_port", 9185),
            host=raw.get("exporter_host", "0.0.0.0"),
            log_level=raw.get("log_level", "INFO"),
            api_version=raw.get("api_version", "2022-11-28"),
        )


@dataclass
class CacheEntry:
    """Cached API response with expiry."""

    data: dict[str, Any]
    expires_at: float


class CopilotPremiumCollector:
    """Custom Prometheus collector for GitHub Copilot premium request billing data.

    Rebuilds the full label set every scrape so stale series disappear when a model
    drops out. API responses are cached for `config.cache_ttl` seconds.
    """

    def __init__(self, config: ExporterConfig, registry: CollectorRegistry | None = REGISTRY):
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
        """Create an HTTP session with retry logic for transient 5xx errors."""
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._config.token}",
                "X-GitHub-Api-Version": self._config.api_version,
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

    def _fetch(self) -> dict[str, Any] | None:
        """Fetch billing data for the configured enterprise, using cache."""
        now = time.monotonic()

        with self._cache_lock:
            if self._cache and self._cache.expires_at > now:
                logger.debug("Cache hit for enterprise/%s", self._config.enterprise)
                return self._cache.data

        url = (
            f"https://api.github.com/enterprises/{self._config.enterprise}"
            f"/settings/billing/premium_request/usage"
        )
        try:
            resp = self._session.get(url, timeout=self._config.http_timeout)
        except requests.RequestException as exc:
            logger.error(
                "HTTP error fetching enterprise/%s: %s", self._config.enterprise, exc
            )
            self._scrape_failures.inc()
            return None

        if resp.status_code == 200:
            data = resp.json()
            with self._cache_lock:
                self._cache = CacheEntry(
                    data=data, expires_at=time.monotonic() + self._config.cache_ttl
                )
            return data

        self._scrape_failures.inc()

        if resp.status_code in (401, 403):
            logger.error(
                "Auth failed for enterprise/%s (HTTP %d). "
                "Classic PAT needs admin:enterprise scope. "
                "Fine-grained token needs Billing: read permission.",
                self._config.enterprise,
                resp.status_code,
            )
        elif resp.status_code == 404:
            logger.error(
                "enterprise/%s returned 404 — it may not be on the Enhanced Billing Platform.",
                self._config.enterprise,
            )
        elif resp.status_code == 429 or resp.headers.get("X-RateLimit-Remaining") == "0":
            reset_at = resp.headers.get("X-RateLimit-Reset", "unknown")
            logger.warning(
                "Rate limited for enterprise/%s. Resets at epoch %s. "
                "Will serve cached data until cache TTL expires.",
                self._config.enterprise,
                reset_at,
            )
        else:
            logger.error(
                "Unexpected HTTP %d for enterprise/%s: %s",
                resp.status_code,
                self._config.enterprise,
                resp.text[:200],
            )
        return None

    def describe(self) -> list[GaugeMetricFamily]:
        """Return empty metric families for registration — avoids a collect() call at startup."""
        return [GaugeMetricFamily(name, "", labels=LABEL_NAMES) for name in USAGE_METRICS]

    def collect(self):
        """Yield Prometheus metric families from cached GitHub billing data."""
        start = time.monotonic()
        data = self._fetch()
        duration = time.monotonic() - start

        self._scrape_duration.observe(duration)

        # Build gauge families — fresh on every scrape, so stale series disappear.
        families: dict[str, GaugeMetricFamily] = {}
        for metric_name in USAGE_METRICS:
            families[metric_name] = GaugeMetricFamily(
                metric_name,
                f"Copilot premium request {USAGE_METRICS[metric_name]}",
                labels=LABEL_NAMES,
            )

        if data:
            year = str(data.get("timePeriod", {}).get("year", ""))
            for item in data.get("usageItems", []):
                label_values = [
                    "enterprise",
                    self._config.enterprise,
                    item.get("product", ""),
                    item.get("sku", ""),
                    item.get("model", ""),
                    item.get("unitType", ""),
                    year,
                ]
                for metric_name, json_key in USAGE_METRICS.items():
                    value = item.get(json_key, 0)
                    families[metric_name].add_metric(label_values, float(value))

        yield from families.values()

        success = GaugeMetricFamily(
            "github_premium_request_scrape_success",
            "Whether the last scrape succeeded (1) or failed (0)",
        )
        success.add_metric([], 1.0 if data is not None else 0.0)
        yield success

        last_scrape = GaugeMetricFamily(
            "github_premium_request_last_scrape_timestamp_seconds",
            "Unix timestamp of the last scrape",
        )
        last_scrape.add_metric([], time.time())
        yield last_scrape


def main() -> None:
    """Entry point: load config.json, register collector, start HTTP server."""
    config = ExporterConfig.load()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info(
        "Starting exporter on %s:%d (enterprise=%s, cache_ttl=%ds)",
        config.host,
        config.port,
        config.enterprise,
        config.cache_ttl,
    )

    CopilotPremiumCollector(config)
    start_http_server(config.port, addr=config.host)
    logger.info("Exporter ready")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
