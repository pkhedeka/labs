# OCP Lab

Tooling for deploying and managing OpenShift UPI clusters on a shared KVM host.

## Components

### ocp-upi-deploy.sh

Automated OpenShift 4.x UPI bare-metal deployment script for KVM/libvirt.

**Usage:**
```bash
sudo ./ocp-upi-deploy.sh <ocp_version> [cluster_name] [ip_offset]
```

**Examples:**
```bash
# Deploy OCP 4.20.1 with default cluster name "upi" and IP offset 110
sudo ./ocp-upi-deploy.sh 4.20.1

# Deploy with custom cluster name and IP offset for parallel clusters
sudo ./ocp-upi-deploy.sh 4.20.1 lab2 150
```

**What it does:**
- Downloads `openshift-install`, `oc`, and RHCOS live ISO (cached per version)
- Verifies SHA256 checksums for all downloads
- Generates ignition configs from `install-config.yaml`
- Creates per-node customized ISOs with static networking
- Adds libvirt DHCP reservations for stable IP assignments
- Provisions 6 VMs (1 bootstrap, 3 masters, 2 workers)
- Waits for bootstrap completion, then removes bootstrap VM
- Runs background CSR approval loop scoped to the cluster
- Prints cluster credentials on completion

**Prerequisites:**
- KVM/libvirt with `default` network active
- `coreos-installer`, `virsh`, `virt-install`, `curl`, `jq`
- Pull secret at `/root/pull-secret.txt` (override with `PULL_SECRET_FILE`)
- SSH public key at `~/.ssh/id_ed25519.pub` (override with `SSH_KEY_FILE`)
- DNS zone configured for `<cluster_name>.example.com`
- HAProxy configured for API (6443), MCS (22623), and ingress (80/443)

### labportal/

Flask web application for managing lab access requests.

**Features:**
- Access request form with @redhat.com email validation
- Admin panel for approving/denying requests
- Email notifications via SMTP
- Live system resource and VM status on homepage
- PatternFly 5 dark theme UI

**Configuration** is via environment variables (see `labportal/config.py`):
- `LABPORTAL_SECRET_KEY` - Flask session secret
- `LABPORTAL_ADMIN_USER` - Admin username (default: `admin`)
- `LABPORTAL_HOSTNAME` - Lab hostname shown in UI
- `LABPORTAL_SMTP_HOST` - SMTP server for notifications

**Setup:**
```bash
cd labportal
pip install -r requirements.txt
python3 app.py set-password   # set admin password
python3 app.py                # run on port 5000
```

## License

Internal tooling.
