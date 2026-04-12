#!/bin/bash
# =============================================================================
# Dynamic MOTD generator — shows all active OCP clusters
#
# Called by ocp-upi-deploy.sh after install, and by a profile.d hook on login.
# Can also be run manually: ./update-motd.sh
# =============================================================================

CLUSTERS_DIR="/kvm/clusters"
MOTD_FILE="/etc/motd"

# Source site config for domain
if [ -f /etc/ocp-lab.conf ]; then
    # shellcheck source=/dev/null
    source /etc/ocp-lab.conf
fi
BASE_DOMAIN="${BASE_DOMAIN:-example.com}"

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

            # Check if API is reachable (quick timeout)
            api_status="Unknown"
            if KUBECONFIG="$kubeconfig" oc get clusterversion 2>/dev/null | grep -q "True"; then
                api_status="Ready"
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
        done
    fi

    echo "#"
    echo "###################################################################"
    echo ""
} > "$MOTD_FILE"

echo "MOTD updated: $(date '+%Y-%m-%d %H:%M:%S')"
