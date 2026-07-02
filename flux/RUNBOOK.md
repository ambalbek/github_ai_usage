# Flux Deployment Runbook: Copilot Premium Exporter

## Prerequisites

- Flux installed on the cluster (`flux bootstrap github --owner=<org> --repository=<repo> ...`)
- A GitHub PAT with repo read access

## Steps

### 1. Build and push the Docker image

```bash
# Login to ACR
az acr login --name <YOUR_ACR_NAME>

# Build and push
docker build -t <YOUR_ACR_NAME>.azurecr.io/copilot-premium-exporter:latest .
docker push <YOUR_ACR_NAME>.azurecr.io/copilot-premium-exporter:latest
```

> **Note:** Update `image.repository` in `flux/copilot_premium_exporter.yml` to match your ACR name.

### 2. Create namespaces

```bash
kubectl create namespace monitoring
kubectl create namespace flux-system  # usually exists after bootstrap
```

### 3. Create secrets

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

### 4. Apply Flux manifests

```bash
kubectl apply -k flux/
```

### 5. Reconcile

```bash
flux reconcile source git copilot-premium-exporter
flux reconcile helmrelease copilot-premium-exporter -n monitoring
```

### 6. Verify

```bash
flux get sources git
flux get helmreleases -n monitoring
kubectl get pods -n monitoring -l app.kubernetes.io/name=copilot-premium-exporter
```

### 7. Test metrics

```bash
kubectl port-forward -n monitoring svc/copilot-premium-exporter 9185:9185
curl http://localhost:9185/metrics
```

## Validation

### Run the validation script

```bash
chmod +x flux/validate.sh
./flux/validate.sh
```

### Validate kustomization

```bash
# Render kustomize output (catches missing resource files, bad references)
kubectl kustomize flux/
# or
kustomize build flux/
```

### Validate Helm chart

```bash
# Lint the chart
helm lint ./helm/copilot-premium-exporter \
  -f ./helm/copilot-premium-exporter/values-local.yaml

# Dry-run template render
helm template cpe ./helm/copilot-premium-exporter \
  -f ./helm/copilot-premium-exporter/values-local.yaml

# Dry-run install against the cluster
helm install cpe ./helm/copilot-premium-exporter \
  -f ./helm/copilot-premium-exporter/values-local.yaml \
  -n monitoring --dry-run
```

### Validate Flux manifests (CI)

```bash
# Validate GitRepository and HelmRelease YAML
kubectl apply --dry-run=client -f flux/git-repository.yaml
kubectl apply --dry-run=client -f flux/copilot_premium_exporter.yml
```

## Flux Status Commands

```bash
# Overall Flux health
flux check

# All Flux resources at a glance
flux get all -A

# GitRepository source status
flux get sources git

# HelmRelease status
flux get helmreleases -n monitoring

# Kustomization status (if using Flux Kustomization)
flux get kustomizations -A
```

## Troubleshooting

### Check status and events

```bash
# HelmRelease details and events
kubectl describe helmrelease copilot-premium-exporter -n monitoring

# GitRepository details and events
kubectl describe gitrepository copilot-premium-exporter -n flux-system

# Pod events and logs
kubectl describe pod -n monitoring -l app.kubernetes.io/name=copilot-premium-exporter
kubectl logs -n monitoring -l app.kubernetes.io/name=copilot-premium-exporter --tail=100
```

### Force re-fetch after pushing changes

```bash
flux suspend helmrelease copilot-premium-exporter -n monitoring
flux resume helmrelease copilot-premium-exporter -n monitoring
flux reconcile source git copilot-premium-exporter
flux reconcile helmrelease copilot-premium-exporter -n monitoring
```

### Secrets management

```bash
# List secrets
kubectl get secrets -n monitoring
kubectl get secrets -n flux-system

# Inspect a secret (base64 encoded)
kubectl get secret github-token -n monitoring -o yaml

# Delete and recreate a secret
kubectl delete secret github-token -n monitoring
kubectl create secret generic github-token \
  --namespace monitoring \
  --from-literal=GITHUB_TOKEN=<GITHUB_PAT>

# Recreate git credentials
flux create secret git github-repo-creds \
  --namespace flux-system \
  --url=https://github.com/ambalbek/github_ai_usage.git \
  --username=git \
  --password=<GITHUB_PAT>
```

### Helm release management

```bash
# List Helm releases
helm list -n monitoring

# Check Helm release history
helm history copilot-premium-exporter -n monitoring

# Rollback to previous revision
helm rollback copilot-premium-exporter <REVISION> -n monitoring

# Uninstall
helm uninstall copilot-premium-exporter -n monitoring
```

### Cleanup / full uninstall

```bash
# Remove Flux resources
kubectl delete -k flux/

# Remove the namespace
kubectl delete namespace monitoring

# Remove git credentials
kubectl delete secret github-repo-creds -n flux-system
```

## Known Gotchas

- **HelmRelease API version**: use `helm.toolkit.fluxcd.io/v2` (not `v2beta2`)
- **Secret names**: must be lowercase RFC 1123 (`github-token`, not `GITHUB_TOKEN`)
- **ServiceMonitor**: disable if Prometheus Operator CRDs aren't installed
- **All changes must be pushed** — Flux reads from the remote repo, not local files
- **Cached charts**: if Flux doesn't pick up changes, suspend/resume the HelmRelease
- **CRDs must exist first**: if using ServiceMonitor or other custom resources, install CRDs before deploying
