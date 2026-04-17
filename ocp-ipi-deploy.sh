#!/bin/bash
set -euo pipefail

# =============================================================================
# OpenShift IPI Bare-Metal Deployment Script (KVM/libvirt + VBMC)
#
# Deploys a compact 3-node (masters-only) IPI cluster using VMs that simulate
# bare-metal servers via VirtualBMC (IPMI). The installer manages bootstrap,
# PXE provisioning (ironic), and load balancing (keepalived) automatically.
#
# Usage: ./ocp-ipi-deploy.sh <ocp_version> [cluster_name] [ip_offset] [network_type]
#   ocp_version  - e.g. 4.17.0
#   cluster_name - optional, defaults to "ipi1"
#   ip_offset    - optional, defaults to 140
#                  API VIP = .offset, Ingress VIP = .offset+1, masters = .offset+2..4
#   network_type - optional, OVNKubernetes (default) or OpenShiftSDN (4.14 and below)
#
# Prerequisites:
#   - VirtualBMC daemon running (vbmcd)
#   - libvirt networks: "default" (baremetal) and "provisioning" (PXE)
#   - Pull secret at /root/pull-secret.txt
#   - SSH key at ~/.ssh/id_ed25519.pub
# =============================================================================

# --- CONFIGURATION ---

if [ -f /etc/ocp-lab.conf ]; then
    # shellcheck source=/dev/null
    source /etc/ocp-lab.conf
fi

BASE_DOMAIN="${BASE_DOMAIN:-example.com}"

VERSION="${1:-}"
CLUSTER_NAME="${2:-ipi1}"
IP_OFFSET="${3:-${IP_OFFSET:-140}}"
NETWORK_TYPE="${4:-OVNKubernetes}"

if [ -z "$VERSION" ]; then
    echo "Usage: $0 <ocp_version> [cluster_name] [ip_offset] [network_type]"
    echo "  cluster_name defaults to 'ipi1' if omitted"
    echo "  ip_offset defaults to 140 (VIPs at .140/.141, masters .142-.144)"
    echo "  network_type defaults to OVNKubernetes (use OpenShiftSDN for 4.14 and below)"
    exit 1
fi

BASE_DIR="/kvm/client_tools/$VERSION"
INSTALL_DIR="/kvm/clusters/${CLUSTER_NAME}-${VERSION}"
MIRROR_URL="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/$VERSION"

# Networking
BRIDGE_IP="192.168.122.1"
PROV_BRIDGE="provisioning"
BM_BRIDGE="virbr0"
PROV_NET_CIDR="192.168.0.0/24"
PROV_BRIDGE_IP="192.168.0.1"

# VIPs — managed by keepalived on the cluster nodes (no HAProxy needed)
API_VIP="192.168.122.${IP_OFFSET}"
INGRESS_VIP="192.168.122.$(( IP_OFFSET + 1 ))"

# MAC address scheme: 52:54:00:<HEX_OFFSET>:01:XX
# Uses :01: in 4th octet to avoid collision with UPI's :00:
MAC_BASE=$(printf "%02x" "$IP_OFFSET")

# VBMC port scheme: 6200 + (IP_OFFSET - 100)
VBMC_PORT_BASE=$(( 6200 + IP_OFFSET - 100 ))
VBMC_USER="admin"
VBMC_PASS="password"

# Per-cluster VM name prefix
VM_PREFIX="vm-${CLUSTER_NAME}"

# Node count — compact 3-node (masters only, schedulable)
NUM_MASTERS=3

# Pull secret and SSH key
PULL_SECRET_FILE="${PULL_SECRET_FILE:-/root/pull-secret.txt}"
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}"

# DNS zone files — IPI uses separate include files (writable by root)
FWD_ZONE="/var/named/ipi-forward.include"
REV_ZONE="/var/named/ipi-reverse.include"

# --- CLEANUP FUNCTION ---
cleanup_ipi() {
    local cluster="$1"
    local prefix="vm-${cluster}"
    echo ""
    echo "Cleaning up IPI cluster: $cluster"

    # Destroy and undefine VMs
    for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
        local vm="${prefix}-master-${i}"
        virsh destroy "$vm" 2>/dev/null || true
        virsh undefine "$vm" --remove-all-storage 2>/dev/null || true
    done
    # IPI may create a bootstrap VM
    virsh destroy "${prefix}-bootstrap" 2>/dev/null || true
    virsh undefine "${prefix}-bootstrap" --remove-all-storage 2>/dev/null || true

    # Stop and delete VBMC entries
    for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
        local vm="${prefix}-master-${i}"
        vbmc stop "$vm" 2>/dev/null || true
        vbmc delete "$vm" 2>/dev/null || true
    done

    # Remove DHCP reservations
    for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
        local bm_mac="52:54:00:${MAC_BASE}:01:$(printf '%02x' $(( 0x11 + i )))"
        virsh net-update default delete ip-dhcp-host \
            "<host mac='$bm_mac'/>" \
            --live --config 2>/dev/null || true
    done

    # Remove DNS records (marked block)
    if [ -f "$FWD_ZONE" ]; then
        sed -i "/^; IPI-START ${cluster}$/,/^; IPI-END ${cluster}$/d" "$FWD_ZONE"
    fi
    if [ -f "$REV_ZONE" ]; then
        sed -i "/^; IPI-START ${cluster}$/,/^; IPI-END ${cluster}$/d" "$REV_ZONE"
    fi
    # Bump serial and reload
    update_dns_serial
    systemctl reload named 2>/dev/null || true

    echo "Cleanup complete for $cluster."
}

# --- HELPER: BUMP DNS SERIAL ---
update_dns_serial() {
    local serial
    serial=$(date +%Y%m%d%H)
    # Serial is in the main zone files, not the IPI includes
    local main_fwd="/var/named/forward.upi.example.com"
    local main_rev="/var/named/reverse.upi.example.com"
    for zone in "$main_fwd" "$main_rev"; do
        if [ -f "$zone" ]; then
            chown root:named "$zone"
            sed -i "s/[0-9]\{10\}\(\s*; serial\)/${serial}\1/" "$zone"
            chown named:named "$zone"
        fi
    done
}

# --- TRAP ---
trap 'echo ""; echo "Caught signal — aborting."' INT TERM

# --- PRE-FLIGHT CHECKS ---
echo "=== Pre-flight checks ==="

preflight_ok=true

# Required commands
for cmd in virsh virt-install vbmc ipmitool curl python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "FAIL: '$cmd' is not installed."
        preflight_ok=false
    fi
done

# Pull secret
if [ ! -f "$PULL_SECRET_FILE" ]; then
    echo "FAIL: Pull secret not found at $PULL_SECRET_FILE"
    preflight_ok=false
fi

# SSH key
if [ ! -f "$SSH_KEY_FILE" ]; then
    echo "FAIL: SSH public key not found at $SSH_KEY_FILE"
    preflight_ok=false
fi

# VBMC daemon
if ! systemctl is-active vbmcd &>/dev/null; then
    echo "FAIL: vbmcd service is not running. Start with: systemctl start vbmcd"
    preflight_ok=false
fi

# libvirt default network
if ! virsh net-info default &>/dev/null; then
    echo "FAIL: libvirt 'default' network does not exist."
    preflight_ok=false
fi

# libvirt provisioning network
if ! virsh net-info provisioning &>/dev/null; then
    echo "FAIL: libvirt 'provisioning' network does not exist."
    preflight_ok=false
elif [ "$(virsh net-info provisioning 2>/dev/null | awk '/^Active:/{print $2}')" != "yes" ]; then
    echo "FAIL: libvirt 'provisioning' network is not active."
    preflight_ok=false
fi

# Check for existing VMs with this prefix
for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
    if virsh dominfo "${VM_PREFIX}-master-${i}" &>/dev/null; then
        echo "FAIL: VM '${VM_PREFIX}-master-${i}' already exists. Delete it first or use a different cluster name."
        preflight_ok=false
    fi
done

# Check VBMC ports are free
for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
    local_port=$(( VBMC_PORT_BASE + i ))
    if vbmc list 2>/dev/null | grep -q ":.*${local_port}"; then
        echo "FAIL: VBMC port $local_port already in use."
        preflight_ok=false
    fi
done

# RAM check — 3 masters × 32G = 96G
AVAIL_RAM_KB=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
AVAIL_RAM_GB=$(( AVAIL_RAM_KB / 1024 / 1024 ))
REQUIRED_RAM_GB=90
if [ "$AVAIL_RAM_GB" -lt "$REQUIRED_RAM_GB" ]; then
    echo "WARN: Only ${AVAIL_RAM_GB} GB RAM available, IPI compact cluster needs ~96 GB."
    echo "      Deployment may fail or be very slow."
fi

# os-variant
if command -v osinfo-query &>/dev/null && osinfo-query os | grep -q 'rhel9.0'; then
    OS_VARIANT="rhel9.0"
else
    OS_VARIANT="rhel9-unknown"
fi

if [ "$preflight_ok" = false ]; then
    echo ""
    echo "Pre-flight checks FAILED. Fix the issues above and re-run."
    exit 1
fi

# Ensure VBMC UDP ports are open in the libvirt firewall zone
firewall-cmd --zone=libvirt --query-port=${VBMC_PORT_BASE}-$(( VBMC_PORT_BASE + NUM_MASTERS - 1 ))/udp &>/dev/null || \
    firewall-cmd --zone=libvirt --add-port=${VBMC_PORT_BASE}-$(( VBMC_PORT_BASE + NUM_MASTERS - 1 ))/udp --permanent &>/dev/null
firewall-cmd --zone=libvirt --add-port=${VBMC_PORT_BASE}-$(( VBMC_PORT_BASE + NUM_MASTERS - 1 ))/udp &>/dev/null || true

echo "=== Pre-flight checks passed ==="
echo ""
echo "Cluster:       $CLUSTER_NAME"
echo "OCP Version:   $VERSION"
echo "API VIP:       $API_VIP"
echo "Ingress VIP:   $INGRESS_VIP"
echo "Masters:       192.168.122.$(( IP_OFFSET + 2 )) - 192.168.122.$(( IP_OFFSET + 4 ))"
echo "VBMC ports:    $VBMC_PORT_BASE - $(( VBMC_PORT_BASE + NUM_MASTERS - 1 ))"
echo ""

mkdir -p "$BASE_DIR" "$INSTALL_DIR"

# --- 1. TOOLS MANAGEMENT ---
install_binary() {
    local binary=$1
    sudo cp "$BASE_DIR/$binary" "/usr/local/bin/${binary}.tmp.$$"
    sudo chmod 0755 "/usr/local/bin/${binary}.tmp.$$"
    sudo mv "/usr/local/bin/${binary}.tmp.$$" "/usr/local/bin/$binary"
}

check_and_get_tool() {
    local tool=$1; local binary=$2

    if [[ -f "/usr/local/bin/$binary" ]] && [[ "$($binary version 2>/dev/null)" == *"$VERSION"* ]]; then
        echo "$binary $VERSION is already active."
        return
    fi

    if [[ -f "$BASE_DIR/$binary" ]]; then
        echo "$binary found in cache ($BASE_DIR), installing..."
        install_binary "$binary"
        if [[ "$binary" == "oc" ]] && [[ -f "$BASE_DIR/kubectl" ]]; then
            install_binary "kubectl"
        fi
        return
    fi

    echo "Downloading $tool..."
    curl --fail -SL "$MIRROR_URL/${tool}-linux.tar.gz" -o "$BASE_DIR/${tool}.tar.gz"

    local sha_file="$BASE_DIR/${tool}-sha256.txt"
    if curl --fail -sSL "$MIRROR_URL/sha256sum.txt" -o "$sha_file" 2>/dev/null; then
        local expected
        expected=$(grep "${tool}-linux" "$sha_file" | grep -v arm64 | grep -v ppc64 | grep -v s390x | head -1 | awk '{print $1}' || true)
        if [ -n "$expected" ]; then
            local actual
            actual=$(sha256sum "$BASE_DIR/${tool}.tar.gz" | awk '{print $1}')
            if [ "$expected" != "$actual" ]; then
                echo "FAIL: Checksum mismatch for ${tool}-linux.tar.gz"
                exit 1
            fi
            echo "Checksum verified for $tool."
        fi
    fi

    tar -xzf "$BASE_DIR/${tool}.tar.gz" -C "$BASE_DIR"
    rm -f "$BASE_DIR/${tool}.tar.gz" "$BASE_DIR/${tool}-sha256.txt"

    install_binary "$binary"
    if [[ "$binary" == "oc" ]] && [[ -f "$BASE_DIR/kubectl" ]]; then
        install_binary "kubectl"
    fi
}

check_and_get_tool "openshift-install" "openshift-install"
check_and_get_tool "openshift-client" "oc"

# --- 2. CREATE EMPTY VMs ---
echo ""
echo "=== Creating empty VMs for IPI provisioning ==="

declare -a PROV_MACS=()
declare -a BM_MACS=()

for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
    vm_name="${VM_PREFIX}-master-${i}"
    prov_mac="52:54:00:${MAC_BASE}:01:$(printf '%02x' $(( 0x01 + i )))"
    bm_mac="52:54:00:${MAC_BASE}:01:$(printf '%02x' $(( 0x11 + i )))"
    master_ip="192.168.122.$(( IP_OFFSET + 2 + i ))"
    hostname="master-${i}.${CLUSTER_NAME}.${BASE_DOMAIN}"

    PROV_MACS+=("$prov_mac")
    BM_MACS+=("$bm_mac")

    # Clean up any stale VM with this name
    virsh destroy "$vm_name" 2>/dev/null || true
    virsh undefine "$vm_name" --remove-all-storage 2>/dev/null || true

    # Remove stale SSH host keys
    ssh-keygen -R "$master_ip" 2>/dev/null || true

    echo "Creating VM: $vm_name (prov=$prov_mac, bm=$bm_mac)"
    virt-install --name "$vm_name" \
        --ram 32768 \
        --vcpus 8 \
        --cpu host-passthrough \
        --disk size=120,bus=virtio \
        --network network=provisioning,mac="$prov_mac" \
        --network network=default,mac="$bm_mac" \
        --pxe \
        --boot network,hd \
        --graphics vnc,listen=127.0.0.1 \
        --video virtio \
        --noautoconsole \
        --os-variant "$OS_VARIANT" \
        --noreboot

    # Shut down the VM — virt-install --pxe starts it, but IPI needs them off
    # so the installer can power them on via IPMI/VBMC
    virsh destroy "$vm_name" 2>/dev/null || true

    echo "Adding DHCP reservation: $bm_mac -> $master_ip ($hostname)"
    virsh net-update default add ip-dhcp-host \
        "<host mac='$bm_mac' name='$hostname' ip='$master_ip'/>" \
        --live --config 2>/dev/null || true
done

echo "VMs created successfully."

# --- 3. VBMC SETUP ---
echo ""
echo "=== Setting up VirtualBMC entries ==="

for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
    vm_name="${VM_PREFIX}-master-${i}"
    vbmc_port=$(( VBMC_PORT_BASE + i ))

    # Clean up any stale entry
    vbmc stop "$vm_name" 2>/dev/null || true
    vbmc delete "$vm_name" 2>/dev/null || true

    echo "Creating VBMC: $vm_name on port $vbmc_port"
    vbmc add "$vm_name" \
        --port "$vbmc_port" \
        --address "$PROV_BRIDGE_IP" \
        --username "$VBMC_USER" \
        --password "$VBMC_PASS"

    vbmc start "$vm_name"
done

# Verify VBMC is responding
echo ""
echo "Verifying VBMC connectivity..."
for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
    vbmc_port=$(( VBMC_PORT_BASE + i ))
    vm_name="${VM_PREFIX}-master-${i}"
    status=$(ipmitool -I lanplus -H "$PROV_BRIDGE_IP" -p "$vbmc_port" \
        -U "$VBMC_USER" -P "$VBMC_PASS" power status 2>&1 || true)
    if echo "$status" | grep -qi "off\|on"; then
        echo "  $vm_name (port $vbmc_port): OK — $status"
    else
        echo "  FAIL: $vm_name (port $vbmc_port) — VBMC not responding: $status"
        echo "  Aborting. Check vbmcd and firewall."
        exit 1
    fi
done

# --- 4. DNS RECORDS ---
echo ""
echo "=== Adding DNS records ==="

# Ensure include files exist
touch "$FWD_ZONE" "$REV_ZONE"

# Remove any existing block for this cluster
sed -i "/^; IPI-START ${CLUSTER_NAME}$/,/^; IPI-END ${CLUSTER_NAME}$/d" "$FWD_ZONE"
sed -i "/^; IPI-START ${CLUSTER_NAME}$/,/^; IPI-END ${CLUSTER_NAME}$/d" "$REV_ZONE"

# Forward records
cat >> "$FWD_ZONE" <<DNS_FWD
; IPI-START ${CLUSTER_NAME}
; Cluster: ${CLUSTER_NAME} (IPI compact, offset ${IP_OFFSET})
api.${CLUSTER_NAME}.${BASE_DOMAIN}.		IN	A	${API_VIP}
api-int.${CLUSTER_NAME}.${BASE_DOMAIN}.	IN	A	${API_VIP}
*.apps.${CLUSTER_NAME}.${BASE_DOMAIN}.	IN	A	${INGRESS_VIP}
;
master-0.${CLUSTER_NAME}.${BASE_DOMAIN}.	IN	A	192.168.122.$(( IP_OFFSET + 2 ))
master-1.${CLUSTER_NAME}.${BASE_DOMAIN}.	IN	A	192.168.122.$(( IP_OFFSET + 3 ))
master-2.${CLUSTER_NAME}.${BASE_DOMAIN}.	IN	A	192.168.122.$(( IP_OFFSET + 4 ))
; IPI-END ${CLUSTER_NAME}
;EOF
DNS_FWD

# Reverse PTR records
cat >> "$REV_ZONE" <<DNS_REV
; IPI-START ${CLUSTER_NAME}
$(( IP_OFFSET ))	IN	PTR	api.${CLUSTER_NAME}.${BASE_DOMAIN}.
$(( IP_OFFSET + 1 ))	IN	PTR	ingress.${CLUSTER_NAME}.${BASE_DOMAIN}.
$(( IP_OFFSET + 2 ))	IN	PTR	master-0.${CLUSTER_NAME}.${BASE_DOMAIN}.
$(( IP_OFFSET + 3 ))	IN	PTR	master-1.${CLUSTER_NAME}.${BASE_DOMAIN}.
$(( IP_OFFSET + 4 ))	IN	PTR	master-2.${CLUSTER_NAME}.${BASE_DOMAIN}.
; IPI-END ${CLUSTER_NAME}
;EOF
DNS_REV

# Bump serial on main zone files and reload
update_dns_serial
systemctl reload named
echo "DNS records added and named reloaded."

# Verify DNS
sleep 1
echo "Verifying DNS..."
for record in "api.${CLUSTER_NAME}.${BASE_DOMAIN}" "*.apps.${CLUSTER_NAME}.${BASE_DOMAIN}"; do
    resolved=$(dig +short "$record" @127.0.0.1 2>/dev/null || true)
    if [ -n "$resolved" ]; then
        echo "  $record -> $resolved"
    else
        echo "  WARN: $record did not resolve — check named config"
    fi
done

# --- 5. INSTALL-CONFIG ---
echo ""
echo "=== Generating install-config.yaml ==="
cd "$INSTALL_DIR"

PULL_SECRET=$(cat "$PULL_SECRET_FILE")
SSH_KEY=$(cat "$SSH_KEY_FILE")

# Build hosts YAML block
HOSTS_YAML=""
for i in $(seq 0 $(( NUM_MASTERS - 1 ))); do
    vbmc_port=$(( VBMC_PORT_BASE + i ))
    prov_mac="${PROV_MACS[$i]}"
    HOSTS_YAML+="    - name: master-${i}
      role: master
      bmc:
        address: ipmi://${PROV_BRIDGE_IP}:${vbmc_port}
        username: ${VBMC_USER}
        password: ${VBMC_PASS}
      bootMACAddress: ${prov_mac}
      rootDeviceHints:
        deviceName: /dev/vda
"
done

cat > install-config.yaml <<_INSTALL_CONFIG_
apiVersion: v1
baseDomain: ${BASE_DOMAIN}
metadata:
  name: ${CLUSTER_NAME}
networking:
  networkType: ${NETWORK_TYPE}
  clusterNetwork:
  - cidr: 10.128.0.0/14
    hostPrefix: 23
  serviceNetwork:
  - 172.30.0.0/16
  machineNetwork:
  - cidr: 192.168.122.0/24
compute:
- name: worker
  replicas: 0
controlPlane:
  name: master
  replicas: ${NUM_MASTERS}
  platform:
    baremetal: {}
platform:
  baremetal:
    apiVIPs:
    - ${API_VIP}
    ingressVIPs:
    - ${INGRESS_VIP}
    provisioningNetworkCIDR: ${PROV_NET_CIDR}
    provisioningBridge: ${PROV_BRIDGE}
    externalBridge: ${BM_BRIDGE}
    hosts:
${HOSTS_YAML}
pullSecret: '${PULL_SECRET}'
sshKey: '${SSH_KEY}'
_INSTALL_CONFIG_

cp -f install-config.yaml install-config.yaml_backup
echo "install-config.yaml created."

# --- 6. RUN INSTALLER ---
echo ""
echo "=== Starting IPI installation ==="
echo "This will take approximately 45-60 minutes."
echo "The installer will:"
echo "  1. Create a bootstrap VM and PXE boot the masters via ironic"
echo "  2. Install RHCOS on the masters"
echo "  3. Bootstrap the cluster"
echo "  4. Tear down the bootstrap VM"
echo ""

# Ensure the external bridge (virbr0) is UP — the installer rejects DOWN bridges.
# Start a VM to bring the bridge carrier up; the installer will power-manage
# all VMs via IPMI/VBMC so this is safe.
if [ "$(cat /sys/class/net/${BM_BRIDGE}/operstate 2>/dev/null)" != "up" ]; then
    echo "Bringing up ${BM_BRIDGE} by starting a VM..."
    virsh start "${VM_PREFIX}-master-0" 2>/dev/null || true
    for _w in $(seq 1 5); do
        [ "$(cat /sys/class/net/${BM_BRIDGE}/operstate 2>/dev/null)" = "up" ] && break
        sleep 1
    done
    echo "${BM_BRIDGE} operstate: $(cat /sys/class/net/${BM_BRIDGE}/operstate 2>/dev/null)"
fi

openshift-install create cluster --dir=. --log-level=info

# --- 7. POST-INSTALL ---
export KUBECONFIG="$INSTALL_DIR/auth/kubeconfig"

# Update MOTD
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$SCRIPT_DIR/update-motd.sh" ]; then
    "$SCRIPT_DIR/update-motd.sh"
else
    echo "WARN: update-motd.sh not found — MOTD not updated."
fi

KUBE_PASS=$(cat "$INSTALL_DIR/auth/kubeadmin-password" 2>/dev/null || echo "UNKNOWN")
CONSOLE="console-openshift-console.apps.${CLUSTER_NAME}.${BASE_DOMAIN}"
echo ""
echo "==========================================="
echo "  IPI Cluster ${CLUSTER_NAME} (OCP ${VERSION}) is ready!"
echo "  Type:       Compact 3-node (IPI Baremetal)"
echo "  Console:    https://$CONSOLE"
echo "  Username:   kubeadmin"
echo "  Password:   $KUBE_PASS"
echo "  KUBECONFIG: export KUBECONFIG=$INSTALL_DIR/auth/kubeconfig"
echo "  API VIP:    $API_VIP"
echo "  Ingress:    $INGRESS_VIP"
echo "==========================================="
