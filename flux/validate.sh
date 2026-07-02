#!/bin/bash
# ============================================================
# Flux Deployment Validation Script
# Copilot Premium Exporter
# ============================================================

set -e

NAMESPACE="monitoring"
FLUX_NAMESPACE="flux-system"
RELEASE_NAME="copilot-premium-exporter"
SECRET_NAME="github-token"
GIT_SECRET_NAME="github-repo-creds"

PASS="✓"
FAIL="✗"
WARN="!"
ERRORS=0

check() {
  local description="$1"
  local command="$2"

  if eval "$command" > /dev/null 2>&1; then
    echo "  $PASS $description"
  else
    echo "  $FAIL $description"
    ERRORS=$((ERRORS + 1))
  fi
}

warn_check() {
  local description="$1"
  local command="$2"

  if eval "$command" > /dev/null 2>&1; then
    echo "  $PASS $description"
  else
    echo "  $WARN $description (warning)"
  fi
}

# --- 0. Kustomization Manifest Validation ---
echo ""
echo "=== Kustomization Manifest Validation ==="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUSTOMIZATION_FILE="$SCRIPT_DIR/kustomization.yaml"

if [ -f "$KUSTOMIZATION_FILE" ]; then
  echo "  $PASS kustomization.yaml exists"
else
  echo "  $FAIL kustomization.yaml not found at $KUSTOMIZATION_FILE"
  ERRORS=$((ERRORS + 1))
fi

# Validate kustomize build
if command -v kustomize > /dev/null 2>&1; then
  if kustomize build "$SCRIPT_DIR" > /dev/null 2>&1; then
    echo "  $PASS kustomize build succeeds"
  else
    echo "  $FAIL kustomize build failed"
    kustomize build "$SCRIPT_DIR" 2>&1 | head -5 | sed 's/^/       /'
    ERRORS=$((ERRORS + 1))
  fi
elif kubectl kustomize "$SCRIPT_DIR" > /dev/null 2>&1; then
  echo "  $PASS kubectl kustomize build succeeds"
else
  echo "  $FAIL kubectl kustomize build failed"
  kubectl kustomize "$SCRIPT_DIR" 2>&1 | head -5 | sed 's/^/       /'
  ERRORS=$((ERRORS + 1))
fi

# Validate all resources referenced in kustomization.yaml exist on disk
if [ -f "$KUSTOMIZATION_FILE" ]; then
  MISSING_RESOURCES=0
  while IFS= read -r resource; do
    resource=$(echo "$resource" | sed 's/^[[:space:]]*-[[:space:]]*//' | tr -d '[:space:]')
    [ -z "$resource" ] && continue
    if [ -f "$SCRIPT_DIR/$resource" ]; then
      echo "  $PASS Resource file exists: $resource"
    else
      echo "  $FAIL Resource file missing: $resource"
      MISSING_RESOURCES=$((MISSING_RESOURCES + 1))
    fi
  done < <(grep -A 100 '^resources:' "$KUSTOMIZATION_FILE" | tail -n +2 | grep '^\s*-')
  if [ $MISSING_RESOURCES -gt 0 ]; then
    ERRORS=$((ERRORS + MISSING_RESOURCES))
  fi
fi

# Validate YAML syntax
for file in "$SCRIPT_DIR"/*.yaml "$SCRIPT_DIR"/*.yml; do
  [ -f "$file" ] || continue
  basename=$(basename "$file")

  # Skip kustomization.yaml — it's not a K8s resource, validated by kustomize build above
  if [ "$basename" = "kustomization.yaml" ] || [ "$basename" = "kustomization.yml" ]; then
    echo "  $PASS Valid YAML syntax: $basename (validated via kustomize build)"
    continue
  fi

  if kubectl apply --dry-run=client -f "$file" > /dev/null 2>&1; then
    echo "  $PASS Valid YAML syntax: $basename"
  else
    echo "  $FAIL Invalid YAML syntax or K8s validation failed: $basename"
    kubectl apply --dry-run=client -f "$file" 2>&1 | head -3 | sed 's/^/       /'
    ERRORS=$((ERRORS + 1))
  fi
done

# --- 1. Prerequisites ---
echo ""
echo "=== Prerequisites ==="

check "kubectl is installed" "command -v kubectl"
check "flux CLI is installed" "command -v flux"
check "Flux system is running" "kubectl get namespace $FLUX_NAMESPACE"
check "Monitoring namespace exists" "kubectl get namespace $NAMESPACE"

# --- 2. Flux CRDs ---
echo ""
echo "=== Flux CRDs ==="

check "GitRepository CRD installed" "kubectl get crd gitrepositories.source.toolkit.fluxcd.io"
check "HelmRelease CRD installed" "kubectl get crd helmreleases.helm.toolkit.fluxcd.io"
warn_check "ServiceMonitor CRD installed" "kubectl get crd servicemonitors.monitoring.coreos.com"

# --- 3. Secrets ---
echo ""
echo "=== Secrets ==="

check "Git repo credentials exist ($FLUX_NAMESPACE/$GIT_SECRET_NAME)" \
  "kubectl get secret $GIT_SECRET_NAME -n $FLUX_NAMESPACE"

check "GitHub token secret exists ($NAMESPACE/$SECRET_NAME)" \
  "kubectl get secret $SECRET_NAME -n $NAMESPACE"

check "GitHub token secret has GITHUB_TOKEN key" \
  "kubectl get secret $SECRET_NAME -n $NAMESPACE -o jsonpath='{.data.GITHUB_TOKEN}' | grep -q ."

# --- 4. GitRepository Source ---
echo ""
echo "=== GitRepository Source ==="

check "GitRepository resource exists" \
  "kubectl get gitrepository $RELEASE_NAME -n $FLUX_NAMESPACE"

GIT_READY=$(kubectl get gitrepository "$RELEASE_NAME" -n "$FLUX_NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
if [ "$GIT_READY" = "True" ]; then
  echo "  $PASS GitRepository is ready"
  REVISION=$(kubectl get gitrepository "$RELEASE_NAME" -n "$FLUX_NAMESPACE" -o jsonpath='{.status.artifact.revision}' 2>/dev/null)
  echo "       Revision: $REVISION"
else
  echo "  $FAIL GitRepository is not ready"
  MSG=$(kubectl get gitrepository "$RELEASE_NAME" -n "$FLUX_NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}' 2>/dev/null)
  echo "       Message: $MSG"
  ERRORS=$((ERRORS + 1))
fi

# --- 5. HelmRelease ---
echo ""
echo "=== HelmRelease ==="

check "HelmRelease resource exists" \
  "kubectl get helmrelease $RELEASE_NAME -n $NAMESPACE"

HR_READY=$(kubectl get helmrelease "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
if [ "$HR_READY" = "True" ]; then
  echo "  $PASS HelmRelease is ready"
  VERSION=$(kubectl get helmrelease "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.status.lastAppliedRevision}' 2>/dev/null)
  echo "       Chart version: $VERSION"
else
  echo "  $FAIL HelmRelease is not ready"
  MSG=$(kubectl get helmrelease "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}' 2>/dev/null)
  echo "       Message: $MSG"
  ERRORS=$((ERRORS + 1))
fi

# --- 6. Deployment ---
echo ""
echo "=== Deployment ==="

check "Deployment exists" \
  "kubectl get deployment $RELEASE_NAME -n $NAMESPACE"

READY_REPLICAS=$(kubectl get deployment "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
DESIRED_REPLICAS=$(kubectl get deployment "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.spec.replicas}' 2>/dev/null)

if [ "$READY_REPLICAS" = "$DESIRED_REPLICAS" ] && [ -n "$READY_REPLICAS" ]; then
  echo "  $PASS Pods ready: $READY_REPLICAS/$DESIRED_REPLICAS"
else
  echo "  $FAIL Pods ready: ${READY_REPLICAS:-0}/${DESIRED_REPLICAS:-?}"
  ERRORS=$((ERRORS + 1))
fi

# --- 7. Pod Health ---
echo ""
echo "=== Pod Health ==="

POD=$(kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=copilot-premium-exporter -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -n "$POD" ]; then
  PHASE=$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null)
  if [ "$PHASE" = "Running" ]; then
    echo "  $PASS Pod $POD is running"
  else
    echo "  $FAIL Pod $POD is in phase: $PHASE"
    ERRORS=$((ERRORS + 1))
  fi

  RESTARTS=$(kubectl get pod "$POD" -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
  if [ "${RESTARTS:-0}" -gt 3 ]; then
    echo "  $WARN Pod has $RESTARTS restarts"
  else
    echo "  $PASS Pod restart count: ${RESTARTS:-0}"
  fi
else
  echo "  $FAIL No pods found"
  ERRORS=$((ERRORS + 1))
fi

# --- 8. Service ---
echo ""
echo "=== Service ==="

check "Service exists" \
  "kubectl get svc $RELEASE_NAME -n $NAMESPACE"

PORT=$(kubectl get svc "$RELEASE_NAME" -n "$NAMESPACE" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null)
echo "       Port: ${PORT:-unknown}"

# --- Summary ---
echo ""
echo "============================================"
if [ $ERRORS -eq 0 ]; then
  echo "  $PASS All checks passed"
else
  echo "  $FAIL $ERRORS check(s) failed"
fi
echo "============================================"
echo ""

exit $ERRORS
