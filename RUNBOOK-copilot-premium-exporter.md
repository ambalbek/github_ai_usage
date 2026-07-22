# Runbook: Copilot Premium Exporter (Application Repo)

This runbook covers building, packaging, and validating the copilot-premium-exporter. This repo contains the Docker image and Helm chart. Flux manifests live in the `gitops_flux` repo — see `RUNBOOK-gitops-flux.md`.

## Repo Structure

```
copilot_premium_exporter/
├── copilot_premium_exporter.py   # Exporter source code
├── test_copilot_premium_exporter.py
├── Dockerfile
├── config.json
├── requirements.txt              # prometheus-client, requests, elasticsearch
├── requirements-dev.txt
├── kibana/
│   └── dashboards/
│       └── copilot-premium.ndjson    # Pre-built Kibana dashboard (Lens)
└── helm/copilot-premium-exporter/
    ├── Chart.yaml
    ├── values.yaml
    ├── values-local.yaml
    ├── dashboards/
    │   ├── copilot-premium.json      # Grafana dashboard JSON
    │   └── copilot-premium.ndjson    # Kibana dashboard NDJSON
    └── templates/
        ├── _helpers.tpl
        ├── configmap.yaml
        ├── deployment.yaml
        ├── service.yaml
        ├── servicemonitor.yaml
        ├── grafana-dashboard-cm.yaml
        ├── kibana-dashboard-cm.yaml
        └── secret.yaml
```

---

## Step 1: Prerequisites

- Docker installed
- Helm 3 installed
- Access to a container registry (GHCR or ACR)
- Python 3.11+ (for local dev/testing)

---

## Step 2: Run Tests

```bash
pip install -r requirements-dev.txt
pytest test_copilot_premium_exporter.py -v
```

---

## Step 3: Build and Push Docker Image

### Option A: GitHub Container Registry (GHCR)

```bash
# Login to GHCR
echo $GITHUB_PAT | docker login ghcr.io -u <GITHUB_USERNAME> --password-stdin

# Build and push
docker build -t ghcr.io/<OWNER>/copilot-premium-exporter:<TAG> .
docker push ghcr.io/<OWNER>/copilot-premium-exporter:<TAG>
```

### Option B: Azure Container Registry (ACR)

```bash
# Login to ACR
az acr login --name <ACR_NAME>

# Build and push
docker build -t <ACR_NAME>.azurecr.io/copilot-premium-exporter:<TAG> .
docker push <ACR_NAME>.azurecr.io/copilot-premium-exporter:<TAG>
```

> Replace `<TAG>` with a version tag (e.g., `v1.0.0`) or `latest`.

---

## Step 4: Validate the Helm Chart

### Lint

```bash
helm lint ./helm/copilot-premium-exporter
```

### Template Render (dry-run)

```bash
helm template cpe ./helm/copilot-premium-exporter \
  -f ./helm/copilot-premium-exporter/values.yaml \
  --set serviceMonitor.enabled=true \
  --set grafanaDashboard.enabled=true
```

Verify the output contains:
- Deployment
- Service
- ConfigMap
- ServiceMonitor
- Grafana dashboard ConfigMap

### Dry-run Against Cluster

```bash
helm install cpe ./helm/copilot-premium-exporter \
  -f ./helm/copilot-premium-exporter/values.yaml \
  -n monitoring --dry-run
```

---

## Step 5: Bump Chart Version

When making Helm chart changes, bump the version in `Chart.yaml`:

```yaml
version: 0.2.0  # increment this
```

This ensures Flux detects the chart has changed and triggers an upgrade.

---

## Step 6: Test Locally with Docker Compose

```bash
# Requires GITHUB_TOKEN env var and config.json
export GITHUB_TOKEN=<your-token>
docker compose up -d --build

# Verify metrics
curl http://localhost:9185/metrics

# Verify Prometheus targets
# Open http://localhost:9090/targets

# Verify Grafana dashboard
# Open http://localhost:3000 (admin/admin)
```

---

## Helm Chart Configuration Reference

### values.yaml Key Fields

| Field | Description | Default |
|---|---|---|
| `image.repository` | Container image registry/path | `""` |
| `image.tag` | Image tag | `latest` |
| `config.github_organizations` | List of GitHub orgs to monitor | `[]` |
| `config.github_enterprises` | List of GitHub enterprises to monitor | `[]` |
| `config.exclude_skus` | SKUs to exclude from general endpoint | `[]` |
| `config.cache_ttl_seconds` | Cache TTL for API responses | `900` |
| `config.exporter_port` | Metrics port | `9185` |
| `config.elasticsearch_url` | Elasticsearch URL (empty = disabled) | `""` |
| `config.elasticsearch_index` | ES index / data stream name | `ds-copilot-billing` |
| `elasticsearch.existingSecret` | Secret containing ES_API_KEY | `""` |
| `elasticsearch.apiKeySecretKey` | Key in the secret | `ES_API_KEY` |
| `serviceMonitor.enabled` | Create Prometheus ServiceMonitor | `false` |
| `serviceMonitor.interval` | Scrape interval | `60s` |
| `grafanaDashboard.enabled` | Create Grafana dashboard ConfigMap | `false` |
| `kibanaDashboard.enabled` | Create Kibana dashboard ConfigMap | `false` |
| `existingSecret` | Name of existing secret with GITHUB_TOKEN | `""` |
| `service.type` | Kubernetes Service type | `ClusterIP` |
| `service.port` | Service port | `9185` |

---

## Troubleshooting

### Exporter not starting

```bash
# Check pod logs
kubectl logs -n monitoring -l app.kubernetes.io/name=copilot-premium-exporter --tail=100

# Check pod events
kubectl describe pod -n monitoring -l app.kubernetes.io/name=copilot-premium-exporter
```

### Common issues

- **GITHUB_TOKEN not set**: Secret `github-token` must exist in the `monitoring` namespace with key `GITHUB_TOKEN`
- **Config not mounted**: ConfigMap must be created — check `kubectl get cm -n monitoring`
- **Image pull errors**: Ensure image pull secret exists (`ghcr-secret` or ACR credentials)
- **ServiceMonitor not created**: Verify `serviceMonitor.enabled: true` in values and that Prometheus Operator CRDs are installed (`kubectl get crd servicemonitors.monitoring.coreos.com`)
