# Flux Deployment Runbook: Copilot Premium Exporter

## Prerequisites

- Flux installed on the cluster (`flux bootstrap github --owner=<org> --repository=<repo> ...`)
- A GitHub PAT with repo read access

## Steps

### 1. Create namespaces

```bash
kubectl create namespace monitoring
kubectl create namespace flux-system  # usually exists after bootstrap
```

### 2. Create secrets

Git repo credentials (for Flux to pull the chart):

```bash
flux create secret git github-repo-creds \
  --namespace flux-system \
  --url=https://github.com/ambalbek/github_ai_usage.git \
  --username=git \
  --password=<GITHUB_PAT>
```

GitHub token for the exporter:

```bash
kubectl create secret generic github-token \
  --namespace monitoring \
  --from-literal=GITHUB_TOKEN=<GITHUB_PAT>
```

### 3. Apply Flux manifests

```bash
kubectl apply -k flux/
```

### 4. Reconcile

```bash
flux reconcile source git copilot-premium-exporter
flux reconcile helmrelease copilot-premium-exporter -n monitoring
```

### 5. Verify

```bash
flux get sources git
flux get helmreleases -n monitoring
kubectl get pods -n monitoring -l app.kubernetes.io/name=copilot-premium-exporter
```

### 6. Test metrics

```bash
kubectl port-forward -n monitoring svc/copilot-premium-exporter 9185:9185
curl http://localhost:9185/metrics
```

## Troubleshooting

Check HelmRelease status:

```bash
flux get helmreleases -n monitoring
```

Check GitRepository source:

```bash
flux get sources git
```

Force re-fetch after pushing changes:

```bash
flux suspend helmrelease copilot-premium-exporter -n monitoring
flux resume helmrelease copilot-premium-exporter -n monitoring
flux reconcile source git copilot-premium-exporter
flux reconcile helmrelease copilot-premium-exporter -n monitoring
```

View Helm release events:

```bash
kubectl describe helmrelease copilot-premium-exporter -n monitoring
```

Delete and recreate a secret:

```bash
kubectl delete secret github-token -n monitoring
kubectl create secret generic github-token \
  --namespace monitoring \
  --from-literal=GITHUB_TOKEN=<GITHUB_PAT>
```

## Known Gotchas

- **HelmRelease API version**: use `helm.toolkit.fluxcd.io/v2` (not `v2beta2`)
- **Secret names**: must be lowercase RFC 1123 (`github-token`, not `GITHUB_TOKEN`)
- **ServiceMonitor**: disable if Prometheus Operator CRDs aren't installed
- **All changes must be pushed** — Flux reads from the remote repo, not local files
