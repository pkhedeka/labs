#!/bin/bash
set -euo pipefail

# =============================================================================
# OpenShift UPI Bare-Metal Deployment Script (KVM/libvirt)
#
# Usage: ./create.sh <ocp_version> [cluster_name]
#   ocp_version  - e.g. 4.16.5
#   cluster_name - optional, defaults to "upi". Use distinct names to run
#                  multiple clusters in parallel without collision.
# =============================================================================

# --- CONFIGURATION ---
VERSION="${1:-}"
CLUSTER_NAME="${2:-upi}"

if [ -z "$VERSION" ]; then
    echo "Usage: $0 <ocp_version> [cluster_name]"
    echo "  cluster_name defaults to 'upi' if omitted"
    exit 1
fi

# Use Absolute Paths to prevent path resolution errors
BASE_DIR="/kvm/client_tools/$VERSION"
INSTALL_DIR="/kvm/clusters/${CLUSTER_NAME}-${VERSION}"
MIRROR_URL="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/$VERSION"

# Networking — each cluster gets a unique IP block based on cluster name hash
# Default network: 192.168.122.0/24
BRIDGE_IP="192.168.122.1"
NETMASK="255.255.255.0"

# Derive a per-cluster IP offset (100-240) from the cluster name to avoid collisions
# This gives each cluster its own IP range within the 192.168.122.0/24 subnet
IP_OFFSET=$(( ( $(echo -n "$CLUSTER_NAME" | cksum | awk '{print $1}') % 140 ) + 100 ))

# Derive per-cluster MAC suffix from offset
MAC_BASE=$(printf "%02x" "$IP_OFFSET")

# Per-cluster VM name prefix
VM_PREFIX="${CLUSTER_NAME}"

# Pull secret and SSH key — configurable via environment
PULL_SECRET_FILE="${PULL_SECRET_FILE:-/root/pull-secret.txt}"
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}"

# --- CLEANUP TRAP ---
CSR_PID=""

cleanup() {
    echo ""
    echo "Caught signal — cleaning up..."
    if [ -n "$CSR_PID" ] && kill -0 "$CSR_PID" 2>/dev/null; then
        echo "Stopping CSR approval loop (PID $CSR_PID)..."
        kill -- -"$CSR_PID" 2>/dev/null || kill "$CSR_PID" 2>/dev/null || true
        wait "$CSR_PID" 2>/dev/null || true
    fi
    echo "Cleanup complete."
}

trap cleanup EXIT INT TERM

# --- PRE-FLIGHT CHECKS ---
echo "=== Pre-flight checks ==="

preflight_ok=true

# Required commands
for cmd in openshift-install oc coreos-installer virsh virt-install curl jq python3; do
    # openshift-install and oc will be downloaded, skip if not yet present
    if [[ "$cmd" == "openshift-install" || "$cmd" == "oc" ]]; then
        continue
    fi
    if ! command -v "$cmd" &>/dev/null; then
        echo "FAIL: '$cmd' is not installed."
        preflight_ok=false
    fi
done

# Pull secret
if [ ! -f "$PULL_SECRET_FILE" ]; then
    echo "FAIL: Pull secret not found at $PULL_SECRET_FILE"
    echo "      Set PULL_SECRET_FILE=/path/to/pull-secret.txt to override."
    preflight_ok=false
fi

# SSH key
if [ ! -f "$SSH_KEY_FILE" ]; then
    echo "FAIL: SSH public key not found at $SSH_KEY_FILE"
    echo "      Set SSH_KEY_FILE=/path/to/key.pub to override."
    preflight_ok=false
fi

# libvirt default network
if ! virsh net-info default &>/dev/null; then
    echo "FAIL: libvirt 'default' network does not exist."
    preflight_ok=false
elif [ "$(virsh net-info default 2>/dev/null | awk '/^Active:/{print $2}')" != "yes" ]; then
    echo "FAIL: libvirt 'default' network is not active. Run: virsh net-start default"
    preflight_ok=false
fi

# RAM check — need at least 80 GB for the full cluster
TOTAL_RAM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
TOTAL_RAM_GB=$(( TOTAL_RAM_KB / 1024 / 1024 ))
REQUIRED_RAM_GB=80
if [ "$TOTAL_RAM_GB" -lt "$REQUIRED_RAM_GB" ]; then
    echo "WARN: System has ${TOTAL_RAM_GB} GB RAM, cluster needs ~${REQUIRED_RAM_GB} GB."
    echo "      Deployment may fail or be very slow due to swapping."
fi

# Disk space check — need at least 750 GB on /kvm (or wherever INSTALL_DIR lives)
INSTALL_MOUNT=$(df --output=avail -B1G "$( dirname "$INSTALL_DIR" )" 2>/dev/null | tail -1 | tr -d ' ')
if [ -n "$INSTALL_MOUNT" ] && [ "$INSTALL_MOUNT" -lt 750 ]; then
    echo "WARN: Only ${INSTALL_MOUNT} GB available for VM disks. 6 VMs x 120 GB = 720 GB needed."
fi

# os-variant check
if command -v osinfo-query &>/dev/null; then
    if ! osinfo-query os | grep -q 'rhel9.0'; then
        echo "WARN: os-variant 'rhel9.0' not found in osinfo-db."
        echo "      Will attempt 'rhel9-unknown' as fallback."
        OS_VARIANT="rhel9-unknown"
    else
        OS_VARIANT="rhel9.0"
    fi
else
    echo "WARN: osinfo-query not found, defaulting os-variant to 'rhel9.0'."
    OS_VARIANT="rhel9.0"
fi

# DNS sanity check — verify at least the API endpoint resolves
API_HOSTNAME="api.${CLUSTER_NAME}.example.com"
if command -v dig &>/dev/null; then
    if ! dig +short "$API_HOSTNAME" 2>/dev/null | grep -q .; then
        echo "WARN: DNS lookup for '$API_HOSTNAME' returned nothing."
        echo "      Ensure DNS is configured for the cluster."
    fi
elif command -v nslookup &>/dev/null; then
    if ! nslookup "$API_HOSTNAME" &>/dev/null; then
        echo "WARN: DNS lookup for '$API_HOSTNAME' failed."
    fi
fi

if [ "$preflight_ok" = false ]; then
    echo ""
    echo "Pre-flight checks FAILED. Fix the issues above and re-run."
    exit 1
fi

echo "=== Pre-flight checks passed ==="
echo ""

mkdir -p "$BASE_DIR" "$INSTALL_DIR"

# --- 1. TOOLS MANAGEMENT ---
check_and_get_tool() {
    local tool=$1; local binary=$2
    if [[ -f "/usr/local/bin/$binary" ]] && [[ "$($binary version 2>/dev/null)" == *"$VERSION"* ]]; then
        echo "$binary $VERSION is already active."
    else
        echo "Downloading $tool..."
        curl --fail -SL "$MIRROR_URL/${tool}-linux.tar.gz" -o "$BASE_DIR/${tool}.tar.gz"

        # Verify checksum if available
        local sha_file="$BASE_DIR/${tool}-sha256.txt"
        if curl --fail -sSL "$MIRROR_URL/sha256sum.txt" -o "$sha_file" 2>/dev/null; then
            local expected
            expected=$(grep "${tool}-linux.tar.gz" "$sha_file" | awk '{print $1}')
            if [ -n "$expected" ]; then
                local actual
                actual=$(sha256sum "$BASE_DIR/${tool}.tar.gz" | awk '{print $1}')
                if [ "$expected" != "$actual" ]; then
                    echo "FAIL: Checksum mismatch for ${tool}-linux.tar.gz"
                    echo "  Expected: $expected"
                    echo "  Got:      $actual"
                    exit 1
                fi
                echo "Checksum verified for $tool."
            fi
        fi

        tar -xzf "$BASE_DIR/${tool}.tar.gz" -C "$BASE_DIR"

        # Atomic binary replacement using mv (rename syscall).
        # cp truncates the destination in-place — if another process has the binary
        # memory-mapped, it gets SIGBUS/SIGSEGV. mv does an inode swap, so existing
        # processes keep their file descriptor to the old inode.
        sudo cp "$BASE_DIR/$binary" "/usr/local/bin/${binary}.tmp.$$"
        sudo chmod 0755 "/usr/local/bin/${binary}.tmp.$$"
        sudo mv "/usr/local/bin/${binary}.tmp.$$" "/usr/local/bin/$binary"

        if [[ "$binary" == "oc" ]]; then
            sudo cp "$BASE_DIR/kubectl" "/usr/local/bin/kubectl.tmp.$$"
            sudo chmod 0755 "/usr/local/bin/kubectl.tmp.$$"
            sudo mv "/usr/local/bin/kubectl.tmp.$$" /usr/local/bin/kubectl
        fi
    fi
}

check_and_get_tool "openshift-install" "openshift-install"
check_and_get_tool "openshift-client" "oc"

# --- 2. LIVE ISO RETRIEVAL ---
echo "Querying RHCOS metadata for x86_64 Live ISO..."
STREAM_JSON=$(openshift-install coreos print-stream-json)

# Parse JSON for the ISO URL — prefer jq, fall back to python3
if command -v jq &>/dev/null; then
    ISO_URL=$(echo "$STREAM_JSON" | jq -r '.architectures.x86_64.artifacts.metal.formats.iso.disk.location')
else
    ISO_URL=$(echo "$STREAM_JSON" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['architectures']['x86_64']['artifacts']['metal']['formats']['iso']['disk']['location'])")
fi

if [ -z "$ISO_URL" ] || [ "$ISO_URL" = "null" ]; then
    echo "FAIL: Could not extract ISO URL from coreos stream metadata."
    exit 1
fi

MASTER_TEMPLATE_ISO="$BASE_DIR/rhcos-live-MASTER.iso"

if [ ! -f "$MASTER_TEMPLATE_ISO" ]; then
    echo "Downloading Master ISO template..."
    curl --fail -SL -o "$MASTER_TEMPLATE_ISO" "$ISO_URL"

    # Verify ISO checksum if available in stream JSON
    if command -v jq &>/dev/null; then
        EXPECTED_SHA=$(echo "$STREAM_JSON" | jq -r '.architectures.x86_64.artifacts.metal.formats.iso.disk.sha256 // empty')
        if [ -n "$EXPECTED_SHA" ]; then
            ACTUAL_SHA=$(sha256sum "$MASTER_TEMPLATE_ISO" | awk '{print $1}')
            if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
                echo "FAIL: ISO checksum mismatch."
                echo "  Expected: $EXPECTED_SHA"
                echo "  Got:      $ACTUAL_SHA"
                rm -f "$MASTER_TEMPLATE_ISO"
                exit 1
            fi
            echo "ISO checksum verified."
        fi
    fi
fi

# --- 3. INSTALL-CONFIG & IGNITION GENERATION ---
cd "$INSTALL_DIR"

echo "Creating install-config.yaml..."

# Pre-read secrets so we don't need command substitution inside the heredoc
PULL_SECRET=$(cat "$PULL_SECRET_FILE")
SSH_KEY=$(cat "$SSH_KEY_FILE")

cat > install-config.yaml <<_INSTALL_CONFIG_
apiVersion: v1
baseDomain: example.com
compute:
- hyperthreading: Enabled
  name: worker
  replicas: 2
controlPlane:
  hyperthreading: Enabled
  name: master
  replicas: 3
metadata:
  name: ${CLUSTER_NAME}
networking:
  clusterNetwork:
  - cidr: 10.128.0.0/14
    hostPrefix: 23
  networkType: OVNKubernetes
  serviceNetwork:
  - 172.30.0.0/16
platform:
  none: {}
fips: false
pullSecret: '${PULL_SECRET}'
sshKey: '${SSH_KEY}'
_INSTALL_CONFIG_

echo "Generating Ignition configs..."
cp -f install-config.yaml install-config.yaml_backup
openshift-install create manifests --dir=.
openshift-install create ignition-configs --dir=.
# Restore backup as the install tool consumes the original
cp install-config.yaml_backup install-config.yaml

# --- 4. PER-NODE CUSTOMIZATION & DEPLOY FUNCTION ---
deploy_node() {
    local name=$1; local ram=$2; local cpu=$3; local mac=$4; local role=$5; local ip=$6; local hostname=$7
    local NODE_ISO="$INSTALL_DIR/${name}.iso"

    echo "--- Creating Node-Specific ISO: $(basename "$NODE_ISO") ---"

    # Customize using the Master as source and Node-specific name as output
    coreos-installer iso customize \
        --dest-ignition "$INSTALL_DIR/$role.ign" \
        --dest-device /dev/vda \
        --dest-console tty0 \
        --dest-console ttyS0,115200n8 \
        --dest-karg-append "ip=$ip::$BRIDGE_IP:$NETMASK:$hostname::none" \
        --dest-karg-append "nameserver=$BRIDGE_IP" \
        -o "$NODE_ISO" "$MASTER_TEMPLATE_ISO"

    if [ ! -f "$NODE_ISO" ]; then
        echo "FAIL: ISO creation failed for $name"
        exit 1
    fi

    echo "Provisioning VM: $name"
    virsh destroy "$name" 2>/dev/null || true
    virsh undefine "$name" --remove-all-storage 2>/dev/null || true

    virt-install --name "$name" \
        --ram "$ram" \
        --vcpus "$cpu" \
        --cpu host-passthrough \
        --disk size=120,bus=virtio \
        --network network=default,mac="$mac" \
        --graphics vnc,listen=0.0.0.0 \
        --video virtio \
        --cdrom "$NODE_ISO" \
        --boot hd,cdrom \
        --noautoconsole \
        --check disk_size=off \
        --os-variant "$OS_VARIANT"
}

# --- 5. EXECUTION LOOP ---
# Per-cluster VM names, MACs, and IPs derived from CLUSTER_NAME + IP_OFFSET
# Usage: Name | RAM | CPU | MAC | Role | IP | Hostname
deploy_node "${VM_PREFIX}-boot" 16384 4 "52:54:00:${MAC_BASE}:00:10" "bootstrap" "192.168.122.$(( IP_OFFSET ))"     "bootstrap.${CLUSTER_NAME}.example.com"
deploy_node "${VM_PREFIX}-m0"   16384 4 "52:54:00:${MAC_BASE}:00:11" "master"    "192.168.122.$(( IP_OFFSET + 1 ))" "master-0.${CLUSTER_NAME}.example.com"
deploy_node "${VM_PREFIX}-m1"   16384 4 "52:54:00:${MAC_BASE}:00:12" "master"    "192.168.122.$(( IP_OFFSET + 2 ))" "master-1.${CLUSTER_NAME}.example.com"
deploy_node "${VM_PREFIX}-m2"   16384 4 "52:54:00:${MAC_BASE}:00:13" "master"    "192.168.122.$(( IP_OFFSET + 3 ))" "master-2.${CLUSTER_NAME}.example.com"
deploy_node "${VM_PREFIX}-w0"   8192  2 "52:54:00:${MAC_BASE}:00:14" "worker"    "192.168.122.$(( IP_OFFSET + 4 ))" "worker-0.${CLUSTER_NAME}.example.com"
deploy_node "${VM_PREFIX}-w1"   8192  2 "52:54:00:${MAC_BASE}:00:15" "worker"    "192.168.122.$(( IP_OFFSET + 5 ))" "worker-1.${CLUSTER_NAME}.example.com"

# --- 6. MONITORING & CSR APPROVAL ---
export KUBECONFIG="$INSTALL_DIR/auth/kubeconfig"

echo "Waiting for Bootstrap (this takes approx 20 mins)..."
openshift-install wait-for bootstrap-complete --dir=. --log-level=info

echo "Deleting Bootstrap VM to reclaim RAM..."
virsh destroy "${VM_PREFIX}-boot" 2>/dev/null && virsh undefine "${VM_PREFIX}-boot" --remove-all-storage 2>/dev/null

echo "Starting background CSR approval loop..."
# Run in a process group (set -m) so we can kill the entire group on cleanup
(
    set +e
    while true; do
        # Only approve CSRs from nodes matching this cluster's hostnames
        for csr in $(oc get csr -o jsonpath='{.items[?(@.status == {})].metadata.name}' 2>/dev/null); do
            requesting_node=$(oc get csr "$csr" -o jsonpath='{.spec.username}' 2>/dev/null)
            if echo "$requesting_node" | grep -q "${CLUSTER_NAME}"; then
                oc adm certificate approve "$csr" 2>/dev/null
            fi
        done
        sleep 30
    done
) &
CSR_PID=$!

echo "Waiting for Final Installation..."
openshift-install wait-for install-complete --dir=. --log-level=info

# Stop background CSR loop (trap will also handle this on exit)
if [ -n "$CSR_PID" ] && kill -0 "$CSR_PID" 2>/dev/null; then
    kill "$CSR_PID" 2>/dev/null || true
    wait "$CSR_PID" 2>/dev/null || true
fi
CSR_PID=""

# --- 7. FINAL BANNER ---
KUBE_PASS=$(cat "$INSTALL_DIR/auth/kubeadmin-password" 2>/dev/null || echo "UNKNOWN")
CONSOLE=$(oc get route -n openshift-console console -o jsonpath='{.spec.host}' 2>/dev/null || echo "console-openshift-console.apps.${CLUSTER_NAME}.example.com")

cat <<_MOTD_ | sudo tee /etc/motd
#########################################################################
#  OPENSHIFT $VERSION DEPLOYMENT COMPLETE  (cluster: $CLUSTER_NAME)
#########################################################################
#  Console URL:  https://$CONSOLE
#  Username:     kubeadmin
#  Password:     $KUBE_PASS
#
#  KUBECONFIG:   export KUBECONFIG=$INSTALL_DIR/auth/kubeconfig
#########################################################################
_MOTD_

echo "Installation complete. Cluster access details written to /etc/motd."
