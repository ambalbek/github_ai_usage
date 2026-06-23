# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Python Prometheus exporter that collects GitHub Copilot AI Credits / Premium Request billing data from the GitHub Enhanced Billing Platform and exposes it as Prometheus metrics. Includes a Grafana dashboard and Helm chart for Kubernetes deployment.

## Commands

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run tests
pytest test_copilot_premium_exporter.py -v

# Run exporter standalone (requires GITHUB_TOKEN env var and config.json)
python copilot_premium_exporter.py

# Docker Compose (exporter + Prometheus + Grafana)
docker compose up -d --build

# Verify metrics
curl http://localhost:9185/metrics
```

## Architecture

**Single-file exporter** (`copilot_premium_exporter.py`) — all logic lives here:

- `ExporterConfig` — dataclass loaded from `config.json` + env vars. Supports multiple orgs and enterprises. Token comes exclusively from `GITHUB_TOKEN` env var.
- `CopilotPremiumCollector` — custom Prometheus collector registered with the default registry. On each Prometheus scrape:
  1. Checks in-memory cache (TTL-based, default 15min). If fresh, returns cached data.
  2. On cache miss, fetches billing data for each configured entity via three endpoints in priority order:
     - `ai_credit/usage` (new billing model) — always called
     - `premium_request/usage` (legacy) — always called
     - `usage` (general fallback) — only called if both above return empty items
  3. Results are tagged with entity metadata, deduplicated by `(model, sku, product, unitType, year, month)`, and converted to `GaugeMetricFamily` instances.
- Operational metrics (scrape duration histogram, failure counter, success gauge) are separate from usage gauges.

**Test file** (`test_copilot_premium_exporter.py`) uses `responses` library to mock HTTP calls. Tests construct `ExporterConfig` directly (bypassing `load()`) and use isolated `CollectorRegistry` instances.

**Config resolution order**: `config.json` fields → env vars `GITHUB_ORGS`/`GITHUB_ENTERPRISES` override. Legacy singular keys (`github_enterprise`, `github_organization`) are accepted alongside plural forms.

**Deployment**: Docker Compose for local dev stack (exporter + Prometheus + Grafana with auto-provisioned dashboard). Helm chart in `helm/copilot-premium-exporter/` for Kubernetes.

## Key Details

- Metric prefix: `github_premium_request_` for all metrics
- Default port: 9185
- Seat-license SKUs (`Copilot Business`, `Copilot Enterprise`) are excluded from the general-endpoint fallback via `exclude_skus` config
- The exporter uses `urllib3` retry (3 retries with backoff on 5xx) built into the requests session
- All label names: `type`, `name`, `product`, `sku`, `model`, `unit`, `year`, `month`
