# GitHub Copilot Premium Request — Prometheus Exporter + Elasticsearch

A Python exporter that collects Copilot AI Credits usage from GitHub's Enhanced Billing Platform and exposes it as Prometheus metrics **and optionally indexes into Elasticsearch**. Includes pre-built Grafana and Kibana dashboards.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Project Structure](#project-structure)
- [Step 1 — Create a GitHub Token](#step-1--create-a-github-token)
- [Step 2 — Configure the Exporter](#step-2--configure-the-exporter)
- [Step 3 — Run with Docker Compose](#step-3--run-with-docker-compose)
- [Step 4 — Run without Docker (standalone)](#step-4--run-without-docker-standalone)
- [Step 5 — Open Grafana Dashboard](#step-5--open-grafana-dashboard)
- [Configuration Reference](#configuration-reference)
- [Metrics Reference](#metrics-reference)
- [Example PromQL Queries](#example-promql-queries)
- [Elasticsearch Integration (Optional)](#elasticsearch-integration-optional)
- [Kibana Dashboard](#kibana-dashboard)
- [Local Testing](#local-testing)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

---

## Prerequisites

- **Python 3.12+** (for standalone mode)
- **Docker** and **Docker Compose** (for containerized mode)
- A **GitHub Personal Access Token** with billing read access
- Your GitHub organization must be on the **Enhanced Billing Platform**

---

## Project Structure

```
.
├── config.json                          # Exporter configuration (edit this)
├── copilot_premium_exporter.py          # Main exporter script
├── test_copilot_premium_exporter.py     # Test suite
├── requirements.txt                     # Runtime dependencies
├── requirements-dev.txt                 # Test dependencies
├── Dockerfile                           # Multi-stage container build
├── docker-compose.yml                   # Full stack: exporter + Prometheus + Grafana
├── prometheus.yml                       # Prometheus scrape config
├── flowchart.html                       # Architecture diagram (open in browser)
├── grafana/
│   ├── dashboards/
│   │   └── copilot-premium.json         # Pre-built Grafana dashboard
│   └── provisioning/
│       ├── datasources/
│       │   └── prometheus.yml           # Auto-configures Prometheus datasource
│       └── dashboards/
│           └── dashboards.yml           # Auto-loads dashboard from file
└── kibana/
    └── dashboards/
        └── copilot-premium.ndjson       # Pre-built Kibana dashboard (Lens)
```

---

## Step 1 — Create a GitHub Token

You need a token that can read billing data. Choose one:

### Option A: Classic Personal Access Token

1. Go to https://github.com/settings/tokens
2. Click **Generate new token (classic)**
3. Select scope: **`manage_billing:copilot`** and **`read:enterprise`**
4. Click **Generate token**
5. Copy the token (starts with `ghp_`)

### Option B: Fine-Grained Personal Access Token (recommended)

1. Go to https://github.com/settings/tokens?type=beta
2. Click **Generate new token**
3. Set **Resource owner** to your enterprise
4. Under **Organization permissions**, set **Billing** to **Read**
5. Click **Generate token**
6. Copy the token (starts with `github_pat_`)

> **Note**: Fine-grained tokens are recommended because they follow the principle of least privilege.

---

## Step 2 — Configure the Exporter

Edit `config.json` and replace `my-enterprise` with your GitHub Enterprise slug:

```json
{
    "github_enterprise": "my-enterprise",
    "api_version": "2022-11-28",
    "cache_ttl_seconds": 900,
    "http_timeout_seconds": 30,
    "exporter_port": 9185,
    "exporter_host": "0.0.0.0",
    "log_level": "INFO"
}
```

> Your enterprise slug is the part after `github.com/enterprises/` in your enterprise URL.

Set your token as an environment variable (never put it in config.json):

```bash
# Linux / macOS
export GITHUB_TOKEN=ghp_your_token_here

# Windows PowerShell
$env:GITHUB_TOKEN = "ghp_your_token_here"

# Windows CMD
set GITHUB_TOKEN=ghp_your_token_here
```

---

## Step 3 — Run with Docker Compose

This starts the exporter, Prometheus, and Grafana as a single stack.

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd github_ai_usage

# 2. Set token
export GITHUB_TOKEN=ghp_your_token_here

# 3. Edit config.json with your org name
#    (see Step 2 above)

# 4. Start all services
docker compose up -d

# 5. Check logs
docker compose logs exporter

# 6. Verify metrics are being served
curl http://localhost:9185/metrics
```

### Services and Ports

| Service    | URL                      | Credentials      |
|------------|--------------------------|-------------------|
| Exporter   | http://localhost:9185    | —                 |
| Prometheus | http://localhost:9090    | —                 |
| Grafana    | http://localhost:3000    | admin / admin     |

### Stop the Stack

```bash
docker compose down

# To also delete stored data (Prometheus TSDB, Grafana DB):
docker compose down -v
```

### Rebuild After Code Changes

```bash
docker compose up -d --build
```

---

## Step 4 — Run without Docker (standalone)

If you only want the exporter without Prometheus/Grafana:

```bash
# 1. Create a virtual environment
python -m venv venv
source venv/bin/activate   # Linux/macOS
# venv\Scripts\activate    # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set token and edit config.json
export GITHUB_TOKEN=ghp_your_token_here

# 4. Run
python copilot_premium_exporter.py

# 5. Verify
curl http://localhost:9185/metrics
```

Then point your existing Prometheus at `localhost:9185` by adding to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: "copilot-premium-exporter"
    scrape_interval: 60s
    static_configs:
      - targets: ["localhost:9185"]
```

---

## Step 5 — Open Grafana Dashboard

When using Docker Compose, the dashboard is **auto-provisioned** — no manual import needed.

1. Open http://localhost:3000
2. Log in with **admin / admin** (skip password change if prompted)
3. Go to **Dashboards** in the left sidebar
4. Click **GitHub Copilot Premium Usage**

### Dashboard Panels

| Row               | Panel                        | Type       | What it Shows                              |
|--------------------|------------------------------|------------|--------------------------------------------|
| **Overview**       | Total Net Spend              | Stat       | Sum of net dollar amount across all models  |
|                    | Total Gross Spend            | Stat       | Sum of gross dollar amount                  |
|                    | Total Discount Savings       | Stat       | Sum of discount dollar amount               |
|                    | Total Net Requests           | Stat       | Sum of net request count                    |
| **Spend by Model** | Net Spend by Model          | Pie (donut)| Dollar breakdown per model                  |
|                    | Net Requests by Model        | Pie (donut)| Request count breakdown per model           |
| **Model Details**  | Gross vs Net Spend           | Bar        | Side-by-side comparison per model           |
|                    | Gross vs Net Requests        | Bar        | Side-by-side comparison per model           |
|                    | Price per Unit               | Bar        | Cost per request for each model             |
|                    | Discount Amount              | Bar        | Discount savings per model                  |
| **Full Table**     | All Models — Full Detail     | Table      | Every metric for every model in one view    |
| **Over Time**      | Net Spend (over time)        | Time series| Trend of dollar spend per model             |
|                    | Net Requests (over time)     | Time series| Trend of request count per model            |
| **Exporter Health**| Scrape Status                | Stat       | OK (green) or FAILING (red)                 |
|                    | Scrape Duration              | Stat       | p95 API fetch time                          |
|                    | Total Scrape Failures        | Stat       | Cumulative failure count                    |
|                    | Last Scrape                  | Stat       | Time since last successful data collection  |

### Import Dashboard Manually (if not using Docker Compose)

1. Open Grafana → **Dashboards** → **New** → **Import**
2. Click **Upload dashboard JSON file**
3. Select `grafana/dashboards/copilot-premium.json`
4. Choose your Prometheus datasource
5. Click **Import**

---

## Configuration Reference

### config.json

| Key                    | Required | Default      | Description                                           |
|------------------------|----------|--------------|-------------------------------------------------------|
| `github_enterprise`    | Yes      | —            | Your GitHub Enterprise slug (e.g., `"my-enterprise"`) |
| `api_version`          | No       | `2022-11-28` | GitHub API version header                             |
| `cache_ttl_seconds`    | No       | `900`        | Seconds to cache API responses (15 min default)       |
| `http_timeout_seconds` | No       | `30`         | HTTP request timeout in seconds                       |
| `exporter_port`        | No       | `9185`       | Port the exporter listens on                          |
| `exporter_host`        | No       | `0.0.0.0`    | Host/IP to bind to                                    |
| `log_level`            | No       | `INFO`       | Log verbosity: DEBUG, INFO, WARNING, ERROR, CRITICAL  |
| `elasticsearch_url`    | No       | `""`         | Elasticsearch URL (e.g., `https://es:9200`). Empty = disabled |
| `elasticsearch_index`  | No       | `ds-copilot-billing` | Elasticsearch index / data stream name        |

### Environment Variables

| Variable            | Required | Description                                    |
|---------------------|----------|------------------------------------------------|
| `GITHUB_TOKEN`      | Yes      | GitHub PAT — kept as env var, never in files   |
| `ELASTICSEARCH_URL` | No       | Overrides `elasticsearch_url` in config.json   |
| `ES_API_KEY`        | No       | Elasticsearch API key (enables ES integration) |
| `ES_INDEX`          | No       | Overrides `elasticsearch_index` in config.json |

---

## Metrics Reference

### Usage Metrics (Gauges)

Labels on all usage metrics: `type`, `name`, `product`, `sku`, `model`, `unit`, `year`, `month`

| Metric                                              | Description                        |
|-----------------------------------------------------|------------------------------------|
| `github_premium_request_usage_gross_quantity`        | Total requests before discounts    |
| `github_premium_request_usage_net_quantity`          | Requests after discounts           |
| `github_premium_request_usage_discount_quantity`     | Discounted request count           |
| `github_premium_request_usage_gross_amount`          | Gross dollar amount                |
| `github_premium_request_usage_net_amount`            | Net dollar amount                  |
| `github_premium_request_usage_discount_amount`       | Discount dollar amount             |
| `github_premium_request_usage_price_per_unit`        | Cost per single request            |

### Label Descriptions

| Label     | Example               | Description                                |
|-----------|-----------------------|--------------------------------------------|
| `type`    | `enterprise`          | Always `enterprise`                        |
| `name`    | `my-enterprise`       | Enterprise slug from config.json           |
| `product` | `Copilot`             | GitHub product name                        |
| `sku`     | `Copilot Premium Request` | Billing SKU                            |
| `model`   | `GPT-5`               | AI model used (the key cost dimension)     |
| `unit`    | `requests`            | Unit of measurement                        |
| `year`    | `2026`                | Billing period year                        |
| `month`   | `6`                   | Billing period month (1-12)                |

### Operational Metrics

| Metric                                                | Type      | Description                         |
|-------------------------------------------------------|-----------|-------------------------------------|
| `github_premium_request_scrape_success`               | Gauge     | 1 if last scrape succeeded, 0 if not|
| `github_premium_request_scrape_duration_seconds`      | Histogram | Time spent fetching billing data    |
| `github_premium_request_scrape_failures_total`        | Counter   | Cumulative count of failed scrapes  |
| `github_premium_request_last_scrape_timestamp_seconds`| Gauge     | Unix timestamp of the last scrape   |

---

## Example PromQL Queries

### Current net spend by model
```promql
github_premium_request_usage_net_amount
```

### Total spend across all models
```promql
sum(github_premium_request_usage_net_amount)
```

### Top 5 models by gross cost
```promql
topk(5, github_premium_request_usage_gross_amount)
```

### Discount savings ratio per model
```promql
github_premium_request_usage_discount_amount / github_premium_request_usage_gross_amount
```

### Budget burn rate (net spend change per hour)
```promql
rate(github_premium_request_usage_net_amount[24h]) * 3600
```

### Has the exporter been healthy in the last hour?
```promql
min_over_time(github_premium_request_scrape_success[1h])
```

---

## Elasticsearch Integration (Optional)

The exporter can optionally push billing data to Elasticsearch on each cache refresh. This is **best-effort** — ES failures never break Prometheus metrics.

### Enable Elasticsearch

1. Set the ES URL in `config.json` (or via `ELASTICSEARCH_URL` env var):

```json
{
    "elasticsearch_url": "https://your-es-host:9200",
    "elasticsearch_index": "ds-copilot-billing"
}
```

2. Set the API key as an environment variable:

```bash
export ES_API_KEY=your-elasticsearch-api-key
```

3. Restart the exporter. You should see in logs:

```
Elasticsearch enabled: https://your-es-host:9200 index=ds-copilot-billing
```

### Docker Compose with Elasticsearch

```bash
export GITHUB_TOKEN=ghp_your_token
export ELASTICSEARCH_URL=https://your-es-host:9200
export ES_API_KEY=your-api-key
docker compose up -d --build
```

### Kubernetes (Flux)

Create the ES API key secret:

```bash
kubectl create secret generic elk-apikey \
  --namespace monitoring \
  --from-literal=ES_API_KEY=your-api-key
```

Then in `copilot_premium_exporter.yml`:

```yaml
config:
  elasticsearch_url: "https://your-es-host:9200"
  elasticsearch_index: "ds-copilot-billing"

elasticsearch:
  existingSecret: "elk-apikey"
  apiKeySecretKey: "ES_API_KEY"
```

### Elasticsearch Document Structure

Each billing item is indexed with this structure:

```json
{
  "@timestamp": "2026-07-01T00:00:00Z",
  "entity": {
    "type": "enterprise",
    "name": "my-enterprise"
  },
  "billing": {
    "product": "Copilot",
    "sku": "Copilot AI Credits",
    "model": "GPT-5",
    "unit_type": "ai-credits",
    "gross_quantity": 400,
    "net_quantity": 400,
    "discount_quantity": 0,
    "gross_amount": 4.00,
    "net_amount": 4.00,
    "discount_amount": 0.00,
    "price_per_unit": 0.01
  }
}
```

> **Note**: The data stream template / ILM policy should be configured separately on the Elasticsearch side.

---

## Kibana Dashboard

A pre-built Kibana dashboard is included at `kibana/dashboards/copilot-premium.ndjson`.

### Import into Kibana

1. Open Kibana → **Stack Management** → **Saved Objects**
2. Click **Import**
3. Select `kibana/dashboards/copilot-premium.ndjson`
4. Click **Import**
5. Navigate to **Dashboards** → **GitHub Enterprise — AI Usage by Model (ELK)**

### Dashboard Panels

| Section              | Panel                              | Type           |
|----------------------|------------------------------------|----------------|
| **Overview**         | Net Spend / Gross Spend / Discount / Credits / Requests | Metric |
| **Spend by Model**   | Top Models by Net Spend            | Bar (horizontal) |
|                      | Net Spend Share by Model           | Pie (donut)    |
|                      | Net Spend by Model (Over Time)     | Area (stacked) |
| **Requests by Model**| Net Requests by Model (Over Time)  | Area (stacked) |
|                      | Net Requests Share by Model        | Pie (donut)    |
| **Cost Efficiency**  | Price per Unit by Model            | Bar (horizontal) |
|                      | Net Spend by Entity                | Bar (horizontal) |
| **By SKU**           | Net Spend by SKU (Over Time)       | Area (stacked) |
| **Month Comparison** | Net Spend / Credits by Month       | Bar (vertical) |
|                      | Net Spend by Model — Month Comparison | Bar (stacked) |
| **Detail Tables**    | Per-Model Cost & Usage Breakdown   | Data table     |
|                      | Monthly Summary                    | Data table     |

---

## Local Testing

### Option 1 — Standalone with Elasticsearch

```bash
# 1. Create a virtual environment and install deps
python -m venv venv
source venv/bin/activate   # Linux/macOS
# venv\Scripts\activate    # Windows
pip install -r requirements.txt

# 2. Set all required env vars
export GITHUB_TOKEN=ghp_your_token_here
export ELASTICSEARCH_URL=https://your-es-host:9200
export ES_API_KEY=your-elasticsearch-api-key

# 3. Run the exporter
python copilot_premium_exporter.py

# 4. Verify Prometheus metrics
curl http://localhost:9185/metrics

# 5. Check logs — you should see:
#    "Elasticsearch enabled: https://... index=ds-copilot-billing"
#    "Elasticsearch bulk: N indexed, 0 errors"
```

### Option 2 — Docker Compose with Elasticsearch

```bash
export GITHUB_TOKEN=ghp_your_token_here
export ELASTICSEARCH_URL=https://your-es-host:9200
export ES_API_KEY=your-elasticsearch-api-key

docker compose up -d --build

# Watch exporter logs
docker compose logs -f exporter

# Verify metrics
curl http://localhost:9185/metrics
```

### Option 3 — Prometheus-only (no Elasticsearch)

If you don't set `ELASTICSEARCH_URL` and `ES_API_KEY`, the exporter runs in Prometheus-only mode — same as before the ES integration was added.

```bash
export GITHUB_TOKEN=ghp_your_token_here
python copilot_premium_exporter.py
# Logs will NOT show "Elasticsearch enabled" — that's expected
```

### Option 4 — Unit tests only (no tokens needed)

```bash
pip install -r requirements-dev.txt
pytest test_copilot_premium_exporter.py -v
```

This runs all 30 tests including ES doc structure, bulk send, empty items noop, ES failure resilience, and config loading — no real GitHub token or Elasticsearch cluster needed.

---

## Troubleshooting

### Exporter starts but metrics show no usage data

- **Cause**: Your enterprise may not be on the Enhanced Billing Platform, or there's no Copilot premium usage yet.
- **Check**: Look at exporter logs for 404 errors.
- **Fix**: Confirm your enterprise is enrolled at https://github.com/enterprises/YOUR-ENTERPRISE/settings/billing

### `scrape_success` is 0

- **Cause**: API call failed. Check exporter logs for details.
- Common reasons:
  - **401/403**: Token lacks required scope. Classic PAT needs `manage_billing:copilot` + `read:enterprise`. Fine-grained needs `Billing: read`.
  - **404**: Enterprise not on Enhanced Billing Platform.
  - **429**: Rate limited. The exporter will serve cached data and retry after the cache TTL expires.

### Grafana shows "No data"

1. Check Prometheus is scraping: open http://localhost:9090/targets — the exporter target should show **UP**.
2. Run a test query in Prometheus: `github_premium_request_usage_net_amount` — if empty, the exporter hasn't collected data yet. Wait for the first scrape (up to 60s).
3. In Grafana, verify the datasource: **Settings** → **Data sources** → **Prometheus** → **Test**.

### Elasticsearch shows no data

- **Logs say "Elasticsearch enabled" but no "bulk" line**: The exporter only sends to ES on cache miss. Wait for the cache TTL to expire (default 15 min) or restart the exporter.
- **"Elasticsearch bulk failed: ConnectionError"**: Check `ELASTICSEARCH_URL` is reachable from the exporter. Test with `curl $ELASTICSEARCH_URL`.
- **"Elasticsearch bulk failed: AuthenticationException"**: The `ES_API_KEY` is invalid or expired. Generate a new one in Kibana → Stack Management → API keys.
- **ES not enabled at all**: Both `ELASTICSEARCH_URL` (or `elasticsearch_url` in config.json) **and** `ES_API_KEY` must be set. If either is missing, ES is silently disabled.

### Exporter won't start

- `GITHUB_TOKEN environment variable is required` → Set the env var before running.
- `Failed to read config.json` → Make sure `config.json` exists in the same directory as the script.
- `github_enterprise is required in config.json` → Edit `config.json` and set your enterprise slug.

---

## Development

### Run Tests

```bash
pip install -r requirements-dev.txt
pytest test_copilot_premium_exporter.py -v
```

### Test Coverage

| Test Class              | What it Covers                                      |
|-------------------------|-----------------------------------------------------|
| `TestConstructor`       | Config wiring, auth header, API version header       |
| `TestLoadConfig`        | config.json loading, missing org exits, missing token exits |
| `TestEndpointPriority`  | All three endpoints called, fallback logic, 404 handling |
| `TestCollectWithData`   | Metric families, label values, discount values, success flag |
| `TestCacheTTL`          | Cache hit within TTL, cache miss after TTL           |
| `TestEmptyUsageItems`   | Empty response doesn't crash, still yields families  |
| `TestAuthFailure`       | 401/403 set success=0, increment failure counter     |
| `TestMultiMonth`        | Multi-month fetching, year wrap, months_back=0       |
| `TestExcludeSkus`       | Seat license SKUs excluded from general endpoint     |
| `TestElasticsearchSender` | Doc structure, bulk send, empty noop, ES failure resilience |
| `TestElasticsearchConfig` | ES disabled by default, enabled when configured, config loading |
