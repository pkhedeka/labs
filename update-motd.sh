#!/bin/bash
# =============================================================================
# Dynamic MOTD generator — shows all active OCP clusters
#
# Called by ocp-upi-deploy.sh after install, and by a profile.d hook on login.
# Can also be run manually: ./update-motd.sh
# =============================================================================

MOTD_FILE="/etc/motd"
RESERVATION_FILE="/var/run/cluster-reservations.json"

# Source site config for domain
if [ -f /etc/ocp-lab.conf ]; then
    # shellcheck source=/dev/null
    source /etc/ocp-lab.conf
fi
BASE_DOMAIN="${BASE_DOMAIN:-example.com}"
STORAGE_DIR="${STORAGE_DIR:-/kvm}"
CLUSTERS_DIR="$STORAGE_DIR/clusters"

# Collect active clusters by checking for kubeconfig files
declare -a ACTIVE_CLUSTERS=()

if [ -d "$CLUSTERS_DIR" ]; then
    for dir in "$CLUSTERS_DIR"/*/; do
        [ -d "$dir" ] || continue
        kubeconfig="$dir/auth/kubeconfig"
        [ -f "$kubeconfig" ] || continue

        # Extract cluster name and version from directory name (format: name-version)
        dirname=$(basename "$dir")
        # Check if any VMs are running for this cluster
        cluster_name="${dirname%%-*}"
        version="${dirname#*-}"

        # Match both naming conventions: vm-<cluster>-* and <cluster>-*
        vm_count=$(virsh list --name 2>/dev/null | grep -cE "(vm-)?${cluster_name}-" || true)
        if [ "$vm_count" -gt 0 ]; then
            ACTIVE_CLUSTERS+=("$dirname")
        fi
    done
fi

# Build MOTD
{
    echo ""
    echo "###################################################################"
    echo "#                    OCP Lab — Active Clusters                    #"
    echo "###################################################################"

    if [ ${#ACTIVE_CLUSTERS[@]} -eq 0 ]; then
        echo "#                                                                 #"
        echo "#  No active clusters. Deploy one from the Lab Portal or CLI.     #"
        echo "#                                                                 #"
    else
        for entry in "${ACTIVE_CLUSTERS[@]}"; do
            cluster_name="${entry%%-*}"
            version="${entry#*-}"
            dir="$CLUSTERS_DIR/$entry"
            kubeconfig="$dir/auth/kubeconfig"
            password=$(cat "$dir/auth/kubeadmin-password" 2>/dev/null || echo "N/A")
            console="console-openshift-console.apps.${cluster_name}.${BASE_DOMAIN}"
            vm_count=$(virsh list --name 2>/dev/null | grep -cE "(vm-)?${cluster_name}-" || true)

            # Query live cluster version and status
            api_status="Unknown"
            cv_out=$(KUBECONFIG="$kubeconfig" oc get clusterversion version -o jsonpath='{.status.desired.version}' 2>/dev/null)
            if [ -n "$cv_out" ]; then
                version="$cv_out"
                if KUBECONFIG="$kubeconfig" oc get clusterversion 2>/dev/null | grep -q "True"; then
                    api_status="Ready"
                else
                    api_status="Installing"
                fi
            elif KUBECONFIG="$kubeconfig" oc get nodes &>/dev/null; then
                api_status="Installing"
            else
                api_status="Bootstrapping"
            fi

            echo "#-------------------------------------------------------------------"
            printf "#  %-14s OCP %-10s Status: %-12s VMs: %s\n" "$cluster_name" "$version" "$api_status" "$vm_count"
            echo "#"
            echo "#  Console:    https://$console"
            echo "#  Username:   kubeadmin"
            echo "#  Password:   $password"
            echo "#  KUBECONFIG: export KUBECONFIG=$kubeconfig"
            # Show reservation info if available
            if [ -f "$RESERVATION_FILE" ]; then
                res_info=$(python3 -c "
import json
try:
    d = json.load(open('$RESERVATION_FILE'))
    r = d.get('$cluster_name', {})
    if r:
        who = r.get('reserved_by','').split('@')[0]
        purpose = r.get('purpose','')
        until = r.get('reserved_until','')[:16]
        parts = [who]
        if purpose: parts.append(purpose)
        parts.append('until ' + until)
        print(' | '.join(parts))
except:
    pass
" 2>/dev/null)
                if [ -n "$res_info" ]; then
                    echo "#  RESERVED:   $res_info"
                fi
            fi
        done
    fi

    echo "#"
    echo "###################################################################"
    echo ""
} > "$MOTD_FILE"

echo "MOTD updated: $(date '+%Y-%m-%d %H:%M:%S')"
