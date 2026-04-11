#!/bin/bash

# --- CONFIGURATION ---
VERSION=$1
if [ -z "$VERSION" ]; then
    echo "Usage: $0 <ocp_version>"
    exit 1
fi

# Use Absolute Paths to prevent path resolution errors
BASE_DIR="/kvm/client_tools/$VERSION"
INSTALL_DIR="/kvm/clusters/$VERSION"
MIRROR_URL="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/$VERSION"

# Networking Configuration
BRIDGE_IP="192.168.122.1"
NETMASK="255.255.255.0"

mkdir -p "$BASE_DIR" "$INSTALL_DIR"

# --- 1. TOOLS MANAGEMENT ---
check_and_get_tool() {
    local tool=$1; local binary=$2
    if [[ -f "/usr/local/bin/$binary" ]] && [[ "$($binary version 2>/dev/null)" == *"$VERSION"* ]]; then
        echo "✅ $binary $VERSION is already active."
    else
        echo "📥 Downloading $tool..."
        curl -L "$MIRROR_URL/${tool}-linux.tar.gz" -o "$BASE_DIR/${tool}.tar.gz"
        tar -xzf "$BASE_DIR/${tool}.tar.gz" -C "$BASE_DIR"
        sync && sleep 2
        sudo cp "$BASE_DIR/$binary" /usr/local/bin/
        [[ "$binary" == "oc" ]] && sudo cp "$BASE_DIR/kubectl" /usr/local/bin/
    fi
}

check_and_get_tool "openshift-install" "openshift-install"
check_and_get_tool "openshift-client" "oc"

# --- 2. LIVE ISO RETRIEVAL ---
echo "🔍 Querying RHCOS metadata for x86_64 Live ISO..."
STREAM_JSON=$(openshift-install coreos print-stream-json)

# Parse JSON for the ISO URL (Using the corrected path)
ISO_URL=$(echo "$STREAM_JSON" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['architectures']['x86_64']['artifacts']['metal']['formats']['iso']['disk']['location'])")
MASTER_TEMPLATE_ISO="$BASE_DIR/rhcos-live-MASTER.iso"

if [ ! -f "$MASTER_TEMPLATE_ISO" ]; then
    echo "📥 Downloading Master ISO template..."
    curl -L -o "$MASTER_TEMPLATE_ISO" "$ISO_URL"
fi

# --- 3. INSTALL-CONFIG & IGNITION GENERATION ---
cd "$INSTALL_DIR"

echo "💾 Creating install-config.yaml..."
cat <<EOF > install-config.yaml
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
  name: upi
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
pullSecret: '$(cat /root/pull-secret.txt)'
sshKey: '$(cat ~/.ssh/id_ed25519.pub)'
EOF

echo "⚙️  Generating Ignition configs..."
cp -f install-config.yaml install-config.yaml_backup
openshift-install create manifests --dir=.
openshift-install create ignition-configs --dir=.
# Restore backup as the install tool consumes the original
cp install-config.yaml_backup install-config.yaml

# --- 4. PER-NODE CUSTOMIZATION & DEPLOY FUNCTION ---
deploy_node() {
    local name=$1; local ram=$2; local cpu=$3; local mac=$4; local role=$5; local ip=$6; local hostname=$7
    local NODE_ISO="$INSTALL_DIR/${name}.iso"

    echo "--- 🛠️  Creating Node-Specific ISO: $(basename "$NODE_ISO") ---"
    
    # Customize using the Master as source and Node-specific name as output
    coreos-installer iso customize \
        --dest-ignition "$INSTALL_DIR/$role.ign" \
        --dest-device /dev/vda \
        --dest-console tty0 \
        --dest-console ttyS0,115200n8 \
        --dest-karg-append "ip=$ip::$BRIDGE_IP:$NETMASK:$hostname::none" \
        --dest-karg-append "nameserver=$BRIDGE_IP" \
        -o "$NODE_ISO" "$MASTER_TEMPLATE_ISO"

    # Verification
    if [ ! -f "$NODE_ISO" ]; then echo "❌ ISO creation failed for $name"; exit 1; fi

    echo "🖥️  Provisioning VM: $name"
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
        --os-variant rhel9.0
}

# --- 5. EXECUTION LOOP ---
# Usage: Name | RAM | CPU | MAC | Role | IP | Hostname
deploy_node "ocp-boot" 16384 4 "52:54:00:00:00:10" "bootstrap" "192.168.122.110" "bootstrap.upi.example.com"
deploy_node "ocp-m0"   16384 4 "52:54:00:00:00:11" "master"    "192.168.122.111" "master-0.upi.example.com"
deploy_node "ocp-m1"   16384 4 "52:54:00:00:00:12" "master"    "192.168.122.112" "master-1.upi.example.com"
deploy_node "ocp-m2"   16384 4 "52:54:00:00:00:13" "master"    "192.168.122.113" "master-2.upi.example.com"
deploy_node "ocp-w0"   8192  2 "52:54:00:00:00:14" "worker"    "192.168.122.114" "worker-0.upi.example.com"
deploy_node "ocp-w1"   8192  2 "52:54:00:00:00:15" "worker"    "192.168.122.115" "worker-1.upi.example.com"

# --- 6. MONITORING & CSR APPROVAL ---
export KUBECONFIG="$INSTALL_DIR/auth/kubeconfig"

echo "⏳ Waiting for Bootstrap (this takes approx 20 mins)..."
openshift-install wait-for bootstrap-complete --dir=. --log-level=info

echo "🗑️  Deleting Bootstrap VM to reclaim RAM..."
virsh destroy ocp-boot 2>/dev/null && virsh undefine ocp-boot --remove-all-storage 2>/dev/null

echo "🛡️  Starting background CSR approval loop..."
(
    while true; do
        oc get csr -o name 2>/dev/null | xargs -r oc adm certificate approve 2>/dev/null
        sleep 30
    done
) &
CSR_PID=$!

echo "🚀 Waiting for Final Installation..."
openshift-install wait-for install-complete --dir=. --log-level=info

# Stop background loop
kill $CSR_PID

# --- 7. FINAL BANNER ---
KUBE_PASS=$(cat "$INSTALL_DIR/auth/kubeadmin-password")
CONSOLE=$(oc get route -n openshift-console console -o jsonpath='{.spec.host}' 2>/dev/null)

cat <<EOF | sudo tee /etc/motd
#########################################################################
#  OPENSHIFT $VERSION DEPLOYMENT COMPLETE
#########################################################################
#  Console URL:  https://$CONSOLE
#  Username:     kubeadmin
#  Password:     $KUBE_PASS
#
#  KUBECONFIG:   export KUBECONFIG=$INSTALL_DIR/auth/kubeconfig
#########################################################################
EOF

echo "🏁 Installation complete. Cluster access details written to /etc/motd."
