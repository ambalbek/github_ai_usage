# Runbook: GitOps Flux (Flux Repo)

This runbook covers deploying the copilot-premium-exporter and Grafana to a Kubernetes cluster using Flux. The application image and Helm chart live in the `copilot_premium_exporter` repo — see `RUNBOOK-copilot-premium-exporter.md`.

## Repo Structure

```
gitops_flux/
├── flux-kustomization.yaml          # Flux Kustomization (entry point)
├── kustomization.yaml               # Kustomize resource list
├── git-repository.yaml              # GitRepository source for the exporter chart
├── copilot_premium_exporter.yml     # HelmRelease for the exporter
├── grafana.yml                      # HelmRepository + HelmRelease for Grafana
├── validate.sh                      # Validation script
└── RUNBOOK.md
```

---

## Step 1: Prerequisites

- AKS cluster running
- Flux installed on the cluster
- `kubectl` and `flux` CLI installed
- Prometheus Operator installed (provides ServiceMonitor CRD)
- Access to the `copilot_premium_exporter` Git repo (for Helm chart source)

### Install Flux (if not already bootstrapped)

```bash
flux bootstrap github \
  --owner=<GITHUB_ORG> \
  --repository=gitops_flux \
  --branch=main \
  --path=./ \
  --personal
```

### Verify Flux is running

```bash
flux check
```

---

## Step 2: Create Namespaces

```bash
kubectl create namespace monitoring
kubectl create namespace flux-system  # usually exists after bootstrap
```

---

## Step 3: Create Secrets

### Git repo credentials (for Flux to pull the Helm chart)

```bash
flux create secret git github-repo-creds \
  --namespace flux-system \
  --url=https://github.com/<OWNER>/copilot_premium_exporter.git \
  --username=git \
  --password=<GITHUB_PAT>
```

### GitHub token (for the exporter to call GitHub API)

```bash
kubectl create secret generic github-token \
  --namespace monitoring \
  --from-literal=GITHUB_TOKEN=<GITHUB_PAT>
```

> The PAT needs `read:org` and `read:enterprise` scopes for billing data.

### Elasticsearch API key (optional — only if using ES integration)

```bash
kubectl create secret generic elk-apikey \
  --namespace monitoring \
  --from-literal=ES_API_KEY=<ELASTICSEARCH_API_KEY>
```

### Image pull secret (for GHCR private images)

```bash
kubectl create secret docker-registry ghcr-secret \
  --namespace monitoring \
  --docker-server=ghcr.io \
  --docker-username=<GITHUB_USERNAME> \
  --docker-password=<GITHUB_PAT>
```

> Skip this if your image is public or you use ACR with managed identity.

---

## Step 4: Configure the Exporter HelmRelease

Edit `copilot_premium_exporter.yml` and update these values for your environment:

```yaml
values:
  image:
    repository: ghcr.io/<OWNER>/copilot-premium-exporter  # your registry
    tag: latest                                             # your image tag

  existingSecret: "github-token"  # must match the secret created above

  imagePullSecrets:
    - name: ghcr-secret           # must match the secret created above

  config:
    github_organizations:
      - "your-org-name"           # your GitHub org(s)
    github_enterprises: []        # your GitHub enterprise(s), if any
    exclude_skus:
      - "Copilot Business"
      - "Copilot Enterprise"

  config:
    elasticsearch_url: "https://your-es-host:9200"  # optional — leave empty to disable
    elasticsearch_index: "ds-copilot-billing"

  elasticsearch:
    existingSecret: "elk-apikey"  # optional — secret created above
    apiKeySecretKey: "ES_API_KEY"

  serviceMonitor:
    enabled: true                 # requires Prometheus Operator
    labels: {}                    # add labels if your Prometheus uses serviceMonitorSelector

  grafanaDashboard:
    enabled: true                 # creates ConfigMap for Grafana sidecar

  kibanaDashboard:
    enabled: false                # creates ConfigMap with Kibana NDJSON
```

### ServiceMonitor labels

If Prometheus only watches ServiceMonitors with specific labels, find out which:

```bash
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.serviceMonitorSelector}'
```

If it returns something like `{"matchLabels":{"release":"kube-prometheus-stack"}}`, add that to the values:

```yaml
  serviceMonitor:
    enabled: true
    labels:
      release: kube-prometheus-stack
```

---

## Step 5: Configure Grafana HelmRelease

Edit `grafana.yml` and update for your environment:

```yaml
values:
  datasources:
    datasources.yaml:
      apiVersion: 1
      datasources:
        - name: Prometheus
          type: prometheus
          access: proxy
          url: http://prometheus-operated.monitoring.svc.cluster.local:9090  # adjust if different
          isDefault: true

  service:
    type: LoadBalancer
    port: 80
    annotations:
      service.beta.kubernetes.io/azure-load-balancer-internal: "true"  # internal access only
```

### Find your Prometheus service name

```bash
kubectl get svc -n monitoring | grep prometheus
```

Use the correct service name in the datasource URL.

### Service type options

| Scenario | `service.type` | Annotation needed |
|---|---|---|
| Internal only (AKS) | `LoadBalancer` | `azure-load-balancer-internal: "true"` |
| Public access | `LoadBalancer` | none |
| Port-forward only | `ClusterIP` | none |
| Node IP access | `NodePort` | none |

---

## Step 6: Apply Flux Manifests

### Option A: Let Flux bootstrap pick them up

If Flux is bootstrapped to this repo, just push your changes:

```bash
git add -A && git commit -m "Add exporter and Grafana" && git push
```

### Option B: Apply manually first time

```bash
kubectl apply -f git-repository.yaml
kubectl apply -f flux-kustomization.yaml
```

---

## Step 7: Reconcile and Deploy

```bash
# Reconcile the git source
flux reconcile source git copilot-premium-exporter -n flux-system

# Reconcile the kustomization (deploys both exporter and Grafana)
flux reconcile kustomization copilot-premium-exporter -n flux-system

# Or reconcile individual HelmReleases
flux reconcile helmrelease copilot-premium-exporter -n monitoring
flux reconcile helmrelease grafana -n monitoring
```

---

## Step 8: Verify Deployment

### Check Flux status

```bash
flux get all -A
flux get sources git
flux get helmreleases -n monitoring
flux get kustomizations -A
```

### Check pods

```bash
kubectl get pods -n monitoring
```

Expected pods:
- `copilot-premium-exporter-*`
- `grafana-*`

### Check services

```bash
kubectl get svc -n monitoring
```

### Check ServiceMonitor

```bash
kubectl get servicemonitor -n monitoring
```

---

## Step 9: Verify Prometheus Scraping

```bash
# Port-forward to Prometheus UI
kubectl port-forward -n monitoring svc/prometheus-operated 9090:9090
```

Open `http://localhost:9090/targets` — the copilot-premium-exporter target should show as **UP**.

Test a query:

```promql
github_premium_request_usage
```

---

## Step 10: Access Grafana

### Get the Grafana URL

```bash
kubectl get svc grafana -n monitoring
```

Use the `EXTERNAL-IP` (internal IP if using internal LB) — accessible from your corporate network/VPN.

### Default credentials

- Username: `admin`
- Password: `admin` (you'll be prompted to change on first login)

### Verify dashboard

Navigate to Dashboards > Copilot folder > "Copilot Premium" dashboard. It should show data from Prometheus automatically.

---

## Updating the Deployment

### Update exporter image

In `copilot_premium_exporter.yml`, change the image tag:

```yaml
  image:
    tag: v1.1.0
```

Push and reconcile:

```bash
git push
flux reconcile source git copilot-premium-exporter -n flux-system
flux reconcile helmrelease copilot-premium-exporter -n monitoring
```

### Update exporter config

Change values in `copilot_premium_exporter.yml` (e.g., add orgs, change cache TTL), push, and reconcile.

### Update Grafana

Change values in `grafana.yml`, push, and reconcile:

```bash
flux reconcile helmrelease grafana -n monitoring
```

---

## Force Redeploy

If Flux doesn't pick up changes:

```bash
# Suspend and resume
flux suspend helmrelease <RELEASE_NAME> -n monitoring
flux resume helmrelease <RELEASE_NAME> -n monitoring
flux reconcile helmrelease <RELEASE_NAME> -n monitoring
```

Or fully uninstall and redeploy:

```bash
helm uninstall <RELEASE_NAME> -n monitoring
flux resume helmrelease <RELEASE_NAME> -n monitoring
flux reconcile helmrelease <RELEASE_NAME> -n monitoring
```

---

## Troubleshooting

### HelmRelease not reconciling

```bash
kubectl describe helmrelease <RELEASE_NAME> -n monitoring
kubectl get events -n monitoring --sort-by='.lastTimestamp' | tail -20
```

### Flux source not updating

```bash
flux get source git copilot-premium-exporter -n flux-system
# Compare the revision with your latest commit
git log --oneline -1
```

If revisions don't match:

```bash
flux reconcile source git copilot-premium-exporter -n flux-system
```

### ServiceMonitor exists but not in Prometheus targets

1. Check Prometheus serviceMonitorSelector labels (see Step 4)
2. Check the ServiceMonitor is in the correct namespace
3. Check RBAC — Prometheus needs permission to read ServiceMonitors

```bash
kubectl get prometheus -n monitoring -o yaml | grep -A 5 serviceMonitorSelector
kubectl get servicemonitor -n monitoring -o yaml
```

### Grafana can't reach Prometheus

```bash
# Verify Prometheus service exists
kubectl get svc -n monitoring | grep prometheus

# Test connectivity from Grafana pod
kubectl exec -n monitoring deploy/grafana -- wget -qO- http://prometheus-operated.monitoring.svc.cluster.local:9090/api/v1/status/config
```

### Secrets management

```bash
# List secrets
kubectl get secrets -n monitoring
kubectl get secrets -n flux-system

# Delete and recreate
kubectl delete secret github-token -n monitoring
kubectl create secret generic github-token \
  --namespace monitoring \
  --from-literal=GITHUB_TOKEN=<GITHUB_PAT>

# Recreate git credentials
flux create secret git github-repo-creds \
  --namespace flux-system \
  --url=https://github.com/<OWNER>/copilot_premium_exporter.git \
  --username=git \
  --password=<GITHUB_PAT>
```

---

## Cleanup / Full Uninstall

```bash
# Remove HelmReleases
flux suspend helmrelease copilot-premium-exporter -n monitoring
flux suspend helmrelease grafana -n monitoring
helm uninstall copilot-premium-exporter -n monitoring
helm uninstall grafana -n monitoring

# Remove Flux resources
kubectl delete kustomization copilot-premium-exporter -n flux-system
kubectl delete gitrepository copilot-premium-exporter -n flux-system
kubectl delete helmrepository grafana -n flux-system

# Remove secrets
kubectl delete secret github-token -n monitoring
kubectl delete secret ghcr-secret -n monitoring
kubectl delete secret github-repo-creds -n flux-system

# Remove namespace
kubectl delete namespace monitoring
```

---

## Known Gotchas

- **HelmRelease API version**: use `helm.toolkit.fluxcd.io/v2` (not `v2beta2`)
- **Secret names**: must be lowercase RFC 1123 (`github-token`, not `GITHUB_TOKEN`)
- **Chart version bump**: Flux may not detect Helm chart changes unless `Chart.yaml` version is bumped
- **All changes must be pushed**: Flux reads from the remote repo, not local files
- **ServiceMonitor CRDs**: must be installed before enabling `serviceMonitor.enabled: true`
- **Grafana sidecar**: the dashboard ConfigMap must have label `grafana_dashboard: "1"` to be auto-discovered
- **Internal LB**: the `azure-load-balancer-internal` annotation only works on AKS — other clouds have different annotations
