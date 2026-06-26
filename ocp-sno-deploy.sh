#!/bin/bash
set -euo pipefail

# =============================================================================
# OpenShift SNO (Single Node OpenShift) Deployment Script
#
# Deploys a single-node OpenShift cluster on a remote machine via SSH.
# DNS records are managed on the local (boss) machine.
# VM is created on the remote machine's libvirt.
#
# Usage: ./ocp-sno-deploy.sh <ocp_version> <cluster_name> <target_host> <ssh_user>
# =============================================================================

if [ -f /etc/ocp-lab.conf ]; then
    source /etc/ocp-lab.conf
fi

BASE_DOMAIN="${BASE_DOMAIN:-example.com}"
STORAGE_DIR="${STORAGE_DIR:-/kvm}"
PULL_SECRET_FILE="${PULL_SECRET_FILE:-/root/pull-secret.txt}"
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}"

VERSION="${1:-}"
CLUSTER_NAME="${2:-sno1}"
TARGET_HOST="${3:-}"
SSH_USER="${4:-root}"
SNO_IP="192.168.200.10"
SNO_NETWORK="sno"

if [ -z "$VERSION" ] || [ -z "$TARGET_HOST" ]; then
    echo "Usage: $0 <ocp_version> <cluster_name> <target_host> [ssh_user]"
    exit 1
fi

SSH_CMD="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 ${SSH_USER}@${TARGET_HOST}"
SCP_CMD="scp -o StrictHostKeyChecking=no"

CLUSTER_DIR="${STORAGE_DIR}/clusters/${CLUSTER_NAME}-${VERSION}"
CACHE_DIR="${STORAGE_DIR}/cache/${VERSION}"
REMOTE_IMG_DIR="/kvm/images"

SNO_RAM=16384    # 16G
SNO_VCPUS=8
SNO_DISK=120     # GB

echo "============================================"
echo "SNO Deployment: ${CLUSTER_NAME}"
echo "OCP Version:    ${VERSION}"
echo "Target Host:    ${TARGET_HOST}"
echo "Base Domain:    ${BASE_DOMAIN}"
echo "SNO IP:         ${SNO_IP}"
echo "============================================"

# --- Download openshift-install and oc if not cached ---
mkdir -p "${CACHE_DIR}" "${CLUSTER_DIR}"

MIRROR="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/${VERSION}"

if [ ! -f "${CACHE_DIR}/openshift-install" ]; then
    echo "[1/8] Downloading openshift-install ${VERSION}..."
    curl -sL "${MIRROR}/openshift-install-linux.tar.gz" | tar xz -C "${CACHE_DIR}"
else
    echo "[1/8] openshift-install ${VERSION} cached."
fi

if [ ! -f "${CACHE_DIR}/oc" ]; then
    echo "[2/8] Downloading oc client..."
    curl -sL "${MIRROR}/openshift-client-linux.tar.gz" | tar xz -C "${CACHE_DIR}"
else
    echo "[2/8] oc client cached."
fi

OI="${CACHE_DIR}/openshift-install"

# --- Download RHCOS ISO if not cached ---
if [ ! -f "${CACHE_DIR}/rhcos-live.iso" ]; then
    echo "[3/8] Downloading RHCOS live ISO..."
    RHCOS_URL=$("${OI}" coreos print-stream-json 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print(d['architectures']['x86_64']['artifacts']['metal']['formats']['iso']['disk']['location'])" 2>/dev/null || true)
    if [ -z "$RHCOS_URL" ]; then
        echo "WARNING: Could not determine RHCOS ISO URL, trying default mirror..."
        RHCOS_URL="${MIRROR}/rhcos-live.x86_64.iso"
    fi
    curl -sL -o "${CACHE_DIR}/rhcos-live.iso" "${RHCOS_URL}"
else
    echo "[3/8] RHCOS ISO cached."
fi

# --- Generate install-config.yaml ---
echo "[4/8] Generating install-config.yaml for SNO..."
PULL_SECRET=$(cat "${PULL_SECRET_FILE}")
SSH_KEY=$(cat "${SSH_KEY_FILE}")

cat > "${CLUSTER_DIR}/install-config.yaml" << EOF
apiVersion: v1
baseDomain: ${BASE_DOMAIN}
metadata:
  name: ${CLUSTER_NAME}
networking:
  networkType: OVNKubernetes
  clusterNetwork:
  - cidr: 10.128.0.0/14
    hostPrefix: 23
  machineNetwork:
  - cidr: 192.168.200.0/24
  serviceNetwork:
  - 172.30.0.0/16
compute:
- name: worker
  replicas: 0
controlPlane:
  name: master
  replicas: 1
platform:
  none: {}
bootstrapInPlace:
  installationDisk: /dev/vda
pullSecret: '${PULL_SECRET}'
sshKey: '${SSH_KEY}'
EOF

# Backup install-config (openshift-install consumes it)
cp "${CLUSTER_DIR}/install-config.yaml" "${CLUSTER_DIR}/install-config.yaml.bak"

# --- Generate single-node ignition ---
echo "[5/8] Generating SNO ignition config..."
"${OI}" --dir="${CLUSTER_DIR}" create single-node-ignition-config 2>&1

# --- Embed ignition into RHCOS ISO ---
echo "[6/8] Creating customized ISO with embedded ignition..."
SNO_ISO="${CLUSTER_DIR}/sno-${CLUSTER_NAME}.iso"
if command -v coreos-installer &>/dev/null; then
    coreos-installer iso ignition embed \
        -i "${CLUSTER_DIR}/bootstrap-in-place-for-live-iso.ign" \
        -o "${SNO_ISO}" \
        "${CACHE_DIR}/rhcos-live.iso"
else
    echo "ERROR: coreos-installer not found. Install with: dnf install -y coreos-installer"
    exit 1
fi

# --- Add DNS records on boss machine ---
echo "[7/8] Adding DNS records..."
SNO_ZONE_FILE="/var/named/sno-forward.include"
SNO_REV_FILE="/var/named/sno-reverse.include"

touch "${SNO_ZONE_FILE}" "${SNO_REV_FILE}"

# Remove old records for this cluster
sed -i "/^; SNO-START ${CLUSTER_NAME}$/,/^; SNO-END ${CLUSTER_NAME}$/d" "${SNO_ZONE_FILE}" 2>/dev/null || true
sed -i "/^; SNO-START ${CLUSTER_NAME}$/,/^; SNO-END ${CLUSTER_NAME}$/d" "${SNO_REV_FILE}" 2>/dev/null || true

# Forward records
cat >> "${SNO_ZONE_FILE}" << EOF
; SNO-START ${CLUSTER_NAME}
api.${CLUSTER_NAME}     IN  A  ${TARGET_HOST}
api-int.${CLUSTER_NAME} IN  A  ${TARGET_HOST}
*.apps.${CLUSTER_NAME}  IN  A  ${TARGET_HOST}
; SNO-END ${CLUSTER_NAME}
EOF

# Reload DNS
systemctl reload named 2>/dev/null || true

# --- Transfer ISO and create VM on remote machine ---
echo "[8/8] Deploying VM on ${TARGET_HOST}..."
${SSH_CMD} mkdir -p ${REMOTE_IMG_DIR}
${SCP_CMD} "${SNO_ISO}" "${SSH_USER}@${TARGET_HOST}:${REMOTE_IMG_DIR}/sno-${CLUSTER_NAME}.iso"

# Create VM on remote machine
${SSH_CMD} virt-install \
    --name vm-${CLUSTER_NAME}-master-0 \
    --ram ${SNO_RAM} \
    --vcpus ${SNO_VCPUS} \
    --disk size=${SNO_DISK},pool=kvm,format=qcow2 \
    --cdrom ${REMOTE_IMG_DIR}/sno-${CLUSTER_NAME}.iso \
    --network network=${SNO_NETWORK},mac=52:54:00:a0:00:01 \
    --os-variant rhel9-unknown \
    --boot hd,cdrom \
    --noautoconsole \
    --nographics

echo ""
echo "============================================"
echo "SNO VM created on ${TARGET_HOST}"
echo "============================================"

REMOTE_CLUSTER_DIR="/kvm/clusters/${CLUSTER_NAME}-${VERSION}"

# Copy cluster state and openshift-install to remote machine
${SSH_CMD} mkdir -p ${REMOTE_CLUSTER_DIR}
${SCP_CMD} -r "${CLUSTER_DIR}/auth" "${SSH_USER}@${TARGET_HOST}:${REMOTE_CLUSTER_DIR}/"
${SCP_CMD} "${CLUSTER_DIR}/.openshift_install_state.json" "${SSH_USER}@${TARGET_HOST}:${REMOTE_CLUSTER_DIR}/" 2>/dev/null || true
${SCP_CMD} "${OI}" "${SSH_USER}@${TARGET_HOST}:/usr/local/bin/openshift-install"

# Wait for bootstrap (runs on remote machine where API is reachable)
echo "Waiting for bootstrap to complete..."
${SSH_CMD} "openshift-install --dir=${REMOTE_CLUSTER_DIR} wait-for bootstrap-complete 2>&1" || true

echo "Waiting for install to complete..."
${SSH_CMD} "openshift-install --dir=${REMOTE_CLUSTER_DIR} wait-for install-complete 2>&1" || true

echo ""
echo "============================================"
echo "SNO cluster '${CLUSTER_NAME}' deployment complete!"
echo ""
echo "Access: ssh ${SSH_USER}@${TARGET_HOST}"
echo "KUBECONFIG: ${REMOTE_CLUSTER_DIR}/auth/kubeconfig"
echo "  export KUBECONFIG=${REMOTE_CLUSTER_DIR}/auth/kubeconfig"
echo "============================================"
