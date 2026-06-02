#!/bin/bash
# csr-approver.sh - Auto-approve pending CSRs until cluster is fully installed
#
# Usage: csr-approver.sh <cluster_name>
# Designed to run as systemd service: csr-approver@<cluster>.service
#
# Exits when:
#   - All ClusterOperators are Available (cluster is ready)
#   - Max runtime exceeded (2 hours)
#   - kubeconfig not found

set -euo pipefail

CLUSTER_NAME="${1:?Usage: csr-approver.sh <cluster_name>}"
MAX_RUNTIME=7200  # 2 hours
CHECK_INTERVAL=120  # 2 minutes
START_TIME=$(date +%s)

# Find kubeconfig
KC=$(ls /kvm/clusters/${CLUSTER_NAME}-*/auth/kubeconfig 2>/dev/null | head -1)
if [ -z "$KC" ]; then
    echo "[csr-approver] No kubeconfig found for ${CLUSTER_NAME}, waiting..."
    # Wait up to 30 min for kubeconfig to appear (cluster is still bootstrapping)
    for _i in $(seq 1 60); do
        sleep 30
        KC=$(ls /kvm/clusters/${CLUSTER_NAME}-*/auth/kubeconfig 2>/dev/null | head -1)
        if [ -n "$KC" ]; then
            break
        fi
    done
    if [ -z "$KC" ]; then
        echo "[csr-approver] Timed out waiting for kubeconfig. Exiting."
        exit 1
    fi
fi

export KUBECONFIG="$KC"
echo "[csr-approver] Cluster: ${CLUSTER_NAME}"
echo "[csr-approver] KUBECONFIG: ${KC}"
echo "[csr-approver] Check interval: ${CHECK_INTERVAL}s, max runtime: ${MAX_RUNTIME}s"

approve_pending_csrs() {
    local pending
    pending=$(oc get csr -o go-template='{{range .items}}{{if not .status.certificate}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' 2>/dev/null || true)
    if [ -n "$pending" ]; then
        local count
        count=$(echo "$pending" | wc -l)
        echo "[csr-approver] Approving ${count} pending CSR(s)..."
        echo "$pending" | while read -r csr; do
            oc adm certificate approve "$csr" 2>/dev/null && \
                echo "[csr-approver]   Approved: $csr" || true
        done
    fi
}

is_cluster_ready() {
    # Check if all ClusterOperators are Available=True and not Progressing/Degraded
    local total avail
    total=$(oc get co --no-headers 2>/dev/null | wc -l)
    [ "$total" -eq 0 ] && return 1

    avail=$(oc get co -o go-template='{{range .items}}{{range .status.conditions}}{{if eq .type "Available"}}{{if eq .status "True"}}1{{end}}{{end}}{{end}}{{end}}' 2>/dev/null | tr -d '[:space:]' | wc -c)

    progressing=$(oc get co -o go-template='{{range .items}}{{range .status.conditions}}{{if eq .type "Progressing"}}{{if eq .status "True"}}1{{end}}{{end}}{{end}}{{end}}' 2>/dev/null | tr -d '[:space:]' | wc -c)

    if [ "$avail" -ge "$total" ] && [ "$progressing" -eq 0 ]; then
        return 0
    fi
    return 1
}

while true; do
    elapsed=$(( $(date +%s) - START_TIME ))
    if [ "$elapsed" -ge "$MAX_RUNTIME" ]; then
        echo "[csr-approver] Max runtime (${MAX_RUNTIME}s) reached. Exiting."
        exit 0
    fi

    # Approve any pending CSRs
    approve_pending_csrs

    # Check if cluster is fully installed
    if is_cluster_ready; then
        # Do one final CSR sweep
        sleep 30
        approve_pending_csrs
        echo "[csr-approver] Cluster ${CLUSTER_NAME} is fully installed. All operators Available."
        exit 0
    fi

    remaining=$(( MAX_RUNTIME - elapsed ))
    echo "[csr-approver] Cluster not ready yet. Next check in ${CHECK_INTERVAL}s (${remaining}s remaining)"
    sleep "$CHECK_INTERVAL"
done
