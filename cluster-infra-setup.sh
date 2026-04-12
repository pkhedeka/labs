#!/bin/bash
# =============================================================================
# OCP Lab Portal — DNS (BIND) + HAProxy infrastructure setup
#
# One-time setup for predefined cluster slots (upi1, upi2, upi3).
# After this runs once, no service restarts are needed when deploying
# or deleting clusters — HAProxy health checks handle backend availability.
#
# Usage: ./cluster-infra-setup.sh
#
# Prerequisites: bind (named), haproxy, libvirt must be installed.
# This script will NOT install packages — it only configures them.
# =============================================================================
set -euo pipefail

# Signature used to identify configs managed by this script
MANAGED_BY="# Managed by: OCP Lab Portal (cluster-infra-setup.sh)"

DOMAIN="example.com"
SUBNET="192.168.122"
BRIDGE_IP="${SUBNET}.1"

FORWARD_ZONE="/var/named/forward.upi.example.com"
REVERSE_ZONE="/var/named/reverse.upi.example.com"
HAPROXY_CFG="/etc/haproxy/haproxy.cfg"

# Predefined cluster slots: name -> IP offset
# Each slot uses 6 IPs: offset+0=bootstrap, +1..+3=masters, +4..+5=workers
declare -A CLUSTERS=(
    [upi1]=110
    [upi2]=120
    [upi3]=130
)

# Ordered list for consistent output
CLUSTER_ORDER=(upi1 upi2 upi3)

echo "=== OCP Lab Portal — Infrastructure Setup ==="
echo ""

# --- Pre-flight: check required services are installed ---
echo "=== Checking prerequisites ==="
preflight_ok=true

for svc in named haproxy libvirtd; do
    if ! systemctl list-unit-files "${svc}.service" &>/dev/null; then
        echo "MISSING: ${svc} is not installed."
        echo "  Install it with: dnf install -y ${svc/%d/}"
        preflight_ok=false
    else
        echo "  OK: ${svc} is installed"
    fi
done

for cmd in dig virsh; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "MISSING: '$cmd' command not found."
        case "$cmd" in
            dig) echo "  Install with: dnf install -y bind-utils" ;;
            virsh) echo "  Install with: dnf install -y libvirt" ;;
        esac
        preflight_ok=false
    fi
done

if [ "$preflight_ok" = false ]; then
    echo ""
    echo "STOP: Install missing packages above, then re-run this script."
    exit 1
fi

# --- Check for existing configs not managed by us ---
echo ""
echo "=== Checking existing configurations ==="

check_managed() {
    local file=$1
    local label=$2
    if [ -f "$file" ]; then
        if head -5 "$file" | grep -q "Managed by: OCP Lab Portal"; then
            echo "  ${label}: exists (ours) — will overwrite"
        else
            echo ""
            echo "  WARNING: ${label} exists but was NOT created by this script!"
            echo "  File: ${file}"
            echo "  Review it before proceeding — this script will OVERWRITE it."
            echo ""
            read -rp "  Overwrite ${label}? A backup will be saved. [y/N] " answer
            if [[ ! "$answer" =~ ^[Yy] ]]; then
                echo "  Aborted. Please back up ${file} and re-run."
                exit 1
            fi
            cp "$file" "${file}.bak.$(date +%Y%m%d%H%M%S)"
            echo "  Backup saved: ${file}.bak.*"
        fi
    else
        echo "  ${label}: does not exist — will create"
    fi
}

check_managed "$FORWARD_ZONE" "Forward DNS zone"
check_managed "$REVERSE_ZONE" "Reverse DNS zone"
check_managed "$HAPROXY_CFG" "HAProxy config"

SERIAL=$(date +%Y%m%d%H)

echo ""
echo "=== Generating DNS zone files ==="

# --- Forward zone ---
cat > "$FORWARD_ZONE" << EOF
; ${MANAGED_BY}
\$TTL 1W
@	IN	SOA	ns1.${DOMAIN}.	root (
			${SERIAL}	; serial
			3H		; refresh (3 hours)
			30M		; retry (30 minutes)
			2W		; expiry (2 weeks)
			1W )		; minimum (1 week)
	IN	NS	ns1.${DOMAIN}.
	IN	MX 10	smtp.${DOMAIN}.
;
; Base infrastructure
ns1.${DOMAIN}.		IN	A	${BRIDGE_IP}
smtp.${DOMAIN}.		IN	A	${BRIDGE_IP}
bastion.${DOMAIN}.	IN	A	${BRIDGE_IP}
EOF

for cname in "${CLUSTER_ORDER[@]}"; do
    offset=${CLUSTERS[$cname]}
    cat >> "$FORWARD_ZONE" << EOF
;
; Cluster: ${cname} (offset ${offset})
helper.${cname}.${DOMAIN}.	IN	A	${BRIDGE_IP}
api.${cname}.${DOMAIN}.		IN	A	${BRIDGE_IP}
api-int.${cname}.${DOMAIN}.	IN	A	${BRIDGE_IP}
*.apps.${cname}.${DOMAIN}.	IN	A	${BRIDGE_IP}
;
bootstrap.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset))
master-0.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 1))
master-1.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 2))
master-2.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 3))
worker-0.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 4))
worker-1.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 5))
;
etcd-0.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 1))
etcd-1.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 2))
etcd-2.${cname}.${DOMAIN}.	IN	A	${SUBNET}.$((offset + 3))
_etcd-server-ssl._tcp.${cname}.${DOMAIN}.	86400 IN SRV 0 10 2380 etcd-0.${cname}.${DOMAIN}.
_etcd-server-ssl._tcp.${cname}.${DOMAIN}.	86400 IN SRV 0 10 2380 etcd-1.${cname}.${DOMAIN}.
_etcd-server-ssl._tcp.${cname}.${DOMAIN}.	86400 IN SRV 0 10 2380 etcd-2.${cname}.${DOMAIN}.
EOF
done

echo ";EOF" >> "$FORWARD_ZONE"
echo "  Forward zone written: $FORWARD_ZONE"

# --- Reverse zone ---
cat > "$REVERSE_ZONE" << EOF
; ${MANAGED_BY}
\$TTL 1W
@	IN	SOA	ns1.${DOMAIN}.	root (
			${SERIAL}	; serial
			3H		; refresh (3 hours)
			30M		; retry (30 minutes)
			2W		; expiry (2 weeks)
			1W )		; minimum (1 week)
	IN	NS	ns1.${DOMAIN}.
;
; Base
1	IN	PTR	ns1.${DOMAIN}.
EOF

for cname in "${CLUSTER_ORDER[@]}"; do
    offset=${CLUSTERS[$cname]}
    cat >> "$REVERSE_ZONE" << EOF
;
; Cluster: ${cname} (offset ${offset})
$((offset))	IN	PTR	bootstrap.${cname}.${DOMAIN}.
$((offset + 1))	IN	PTR	master-0.${cname}.${DOMAIN}.
$((offset + 2))	IN	PTR	master-1.${cname}.${DOMAIN}.
$((offset + 3))	IN	PTR	master-2.${cname}.${DOMAIN}.
$((offset + 4))	IN	PTR	worker-0.${cname}.${DOMAIN}.
$((offset + 5))	IN	PTR	worker-1.${cname}.${DOMAIN}.
EOF
done

echo ";EOF" >> "$REVERSE_ZONE"
echo "  Reverse zone written: $REVERSE_ZONE"

# Fix ownership for BIND
chown named:named "$FORWARD_ZONE" "$REVERSE_ZONE"
restorecon "$FORWARD_ZONE" "$REVERSE_ZONE" 2>/dev/null || true

echo ""
echo "=== Generating HAProxy config ==="

# --- HAProxy ---
cat > "$HAPROXY_CFG" << HAHEAD
${MANAGED_BY}
global
  log         127.0.0.1 local2
  pidfile     /var/run/haproxy.pid
  maxconn     4000
  daemon

defaults
  mode                    tcp
  log                     global
  option                  dontlognull
  option                  redispatch
  retries                 3
  timeout http-request    10s
  timeout queue           1m
  timeout connect         10s
  timeout client          1m
  timeout server          1m
  timeout check           10s
  maxconn                 3000
HAHEAD

# API server (6443) — SNI-based routing
{
echo ""
echo "# Kubernetes API — SNI routing to per-cluster backends"
echo "frontend api-server-6443"
echo "  bind ${BRIDGE_IP}:6443"
echo "  mode tcp"
echo "  tcp-request inspect-delay 5s"
echo "  tcp-request content accept if { req_ssl_hello_type 1 }"
for cname in "${CLUSTER_ORDER[@]}"; do
    echo "  use_backend api-${cname}-6443 if { req.ssl_sni -i api.${cname}.${DOMAIN} }"
    echo "  use_backend api-${cname}-6443 if { req.ssl_sni -i api-int.${cname}.${DOMAIN} }"
done
echo "  default_backend api-${CLUSTER_ORDER[0]}-6443"
} >> "$HAPROXY_CFG"

# Machine Config Server (22623) — SNI-based routing
{
echo ""
echo "# Machine Config Server — SNI routing"
echo "frontend mcs-22623"
echo "  bind ${BRIDGE_IP}:22623"
echo "  mode tcp"
echo "  tcp-request inspect-delay 5s"
echo "  tcp-request content accept if { req_ssl_hello_type 1 }"
for cname in "${CLUSTER_ORDER[@]}"; do
    echo "  use_backend mcs-${cname}-22623 if { req.ssl_sni -i api-int.${cname}.${DOMAIN} }"
done
echo "  default_backend mcs-${CLUSTER_ORDER[0]}-22623"
} >> "$HAPROXY_CFG"

# Ingress HTTPS (443) — SNI-based routing
{
echo ""
echo "# Ingress HTTPS — SNI routing for *.apps.<cluster>"
echo "frontend ingress-https-443"
echo "  bind ${BRIDGE_IP}:443"
echo "  mode tcp"
echo "  tcp-request inspect-delay 5s"
echo "  tcp-request content accept if { req_ssl_hello_type 1 }"
for cname in "${CLUSTER_ORDER[@]}"; do
    echo "  use_backend ingress-https-${cname}-443 if { req.ssl_sni -m end .apps.${cname}.${DOMAIN} }"
done
echo "  default_backend ingress-https-${CLUSTER_ORDER[0]}-443"
} >> "$HAPROXY_CFG"

# Ingress HTTP (80) — Host header routing (mode http)
{
echo ""
echo "# Ingress HTTP — Host header routing for *.apps.<cluster>"
echo "frontend ingress-http-80"
echo "  bind ${BRIDGE_IP}:80"
echo "  mode http"
for cname in "${CLUSTER_ORDER[@]}"; do
    echo "  use_backend ingress-http-${cname}-80 if { hdr_end(host) -i .apps.${cname}.${DOMAIN} }"
done
echo "  default_backend ingress-http-${CLUSTER_ORDER[0]}-80"
} >> "$HAPROXY_CFG"

# Per-cluster backends
for cname in "${CLUSTER_ORDER[@]}"; do
    offset=${CLUSTERS[$cname]}
    BS="${SUBNET}.$((offset))"
    M0="${SUBNET}.$((offset + 1))"
    M1="${SUBNET}.$((offset + 2))"
    M2="${SUBNET}.$((offset + 3))"
    W0="${SUBNET}.$((offset + 4))"
    W1="${SUBNET}.$((offset + 5))"

    cat >> "$HAPROXY_CFG" << HABACK

# ---- Cluster: ${cname} (offset ${offset}) ----
backend api-${cname}-6443
  mode tcp
  option httpchk GET /readyz HTTP/1.0
  option log-health-checks
  balance roundrobin
  default-server inter 10s downinter 5s rise 2 fall 3 slowstart 60s maxconn 250 maxqueue 256 weight 100
  server ${cname}-bootstrap ${BS}:6443 check check-ssl verify none backup
  server ${cname}-master-0 ${M0}:6443 check check-ssl verify none
  server ${cname}-master-1 ${M1}:6443 check check-ssl verify none
  server ${cname}-master-2 ${M2}:6443 check check-ssl verify none

backend mcs-${cname}-22623
  mode tcp
  balance roundrobin
  server ${cname}-bootstrap ${BS}:22623 check inter 1s backup
  server ${cname}-master-0 ${M0}:22623 check inter 1s
  server ${cname}-master-1 ${M1}:22623 check inter 1s
  server ${cname}-master-2 ${M2}:22623 check inter 1s

backend ingress-https-${cname}-443
  mode tcp
  balance source
  server ${cname}-worker-0 ${W0}:443 check inter 1s
  server ${cname}-worker-1 ${W1}:443 check inter 1s

backend ingress-http-${cname}-80
  mode http
  balance source
  server ${cname}-worker-0 ${W0}:80 check inter 1s
  server ${cname}-worker-1 ${W1}:80 check inter 1s
HABACK
done

echo "  HAProxy config written: $HAPROXY_CFG"

# Validate HAProxy config
echo ""
echo "=== Validating HAProxy config ==="
if haproxy -c -f "$HAPROXY_CFG"; then
    echo "  Config valid."
else
    echo "  FAIL: HAProxy config validation failed!"
    exit 1
fi

# Reload services
echo ""
echo "=== Reloading services ==="
systemctl reload named || systemctl restart named
echo "  named reloaded."
systemctl reload haproxy || systemctl restart haproxy
echo "  haproxy reloaded."

# Verify DNS
echo ""
echo "=== Verifying DNS resolution ==="
for cname in "${CLUSTER_ORDER[@]}"; do
    offset=${CLUSTERS[$cname]}
    resolved=$(dig +short api.${cname}.${DOMAIN} @127.0.0.1 2>/dev/null || echo "FAILED")
    echo "  api.${cname}.${DOMAIN} -> ${resolved} (expected ${BRIDGE_IP})"
    resolved=$(dig +short bootstrap.${cname}.${DOMAIN} @127.0.0.1 2>/dev/null || echo "FAILED")
    echo "  bootstrap.${cname}.${DOMAIN} -> ${resolved} (expected ${SUBNET}.$((offset)))"
done

echo ""
echo "=== Setup complete ==="
echo "Cluster slots configured:"
for cname in "${CLUSTER_ORDER[@]}"; do
    offset=${CLUSTERS[$cname]}
    echo "  ${cname}: IPs ${SUBNET}.$((offset)) - ${SUBNET}.$((offset + 5))"
done
echo ""
echo "No further service restarts needed — HAProxy health checks handle availability."
echo "Deploy clusters from the portal or CLI using these predefined slot names."
