#!/bin/bash
set -euo pipefail

VERSION=${VERSION:-10.6.1}
BUILD_REVISION=${BUILD_REVISION:-2}
FRR_SERIES=${FRR_SERIES:-10.6}
FRR_DEB_VERSION=${FRR_DEB_VERSION:-${VERSION}-0~deb13u1}
DISK_SIZE_MB=${DISK_SIZE_MB:-4096}
ROOTFS_SIZE_MB=${ROOTFS_SIZE_MB:-$((DISK_SIZE_MB - 64))}
OUT_DIR=${OUT_DIR:-/work/docker}
BUILD_DIR=${BUILD_DIR:-/work/docker/.frr-vm-build}
DEBIAN_SUITE=${DEBIAN_SUITE:-trixie}
DEBIAN_MIRROR=${DEBIAN_MIRROR:-http://deb.debian.org/debian}

ROOTFS="${BUILD_DIR}/rootfs"
ROOTFS_IMG="${BUILD_DIR}/rootfs.ext4"
RAW_DISK="${BUILD_DIR}/installed.raw"
MARKER="${OUT_DIR}/.frr-vm-version"

if [[ -f "${OUT_DIR}/installed.qcow2" \
   && -f "${OUT_DIR}/vmlinuz" \
   && -f "${OUT_DIR}/initrd.img" \
   && -f "${MARKER}" \
   && "$(cat "${MARKER}")" == "${VERSION}:${DISK_SIZE_MB}:${BUILD_REVISION}" ]]; then
  echo "FRR VM disk already prepared for ${VERSION} (${DISK_SIZE_MB} MB)"
  exit 0
fi

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}" "${OUT_DIR}"

export DEBIAN_FRONTEND=noninteractive

apt-get update -qy
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  debootstrap \
  e2fsprogs \
  fdisk \
  gnupg \
  qemu-utils \
  sed \
  util-linux

debootstrap \
  --variant=minbase \
  --include=systemd-sysv,dbus,linux-image-amd64,ca-certificates,curl,gnupg,iproute2,iputils-ping,kmod,less,procps,tcpdump,traceroute,vim-tiny \
  "${DEBIAN_SUITE}" \
  "${ROOTFS}" \
  "${DEBIAN_MIRROR}"

cat > "${ROOTFS}/etc/apt/sources.list.d/frr.list" <<EOF
deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr ${DEBIAN_SUITE} frr-${FRR_SERIES}
EOF
curl -fsSL https://deb.frrouting.org/frr/keys.gpg \
  -o "${ROOTFS}/usr/share/keyrings/frrouting.gpg"

cat > "${ROOTFS}/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod 0755 "${ROOTFS}/usr/sbin/policy-rc.d"

chroot "${ROOTFS}" apt-get update -qy
chroot "${ROOTFS}" apt-get install -y --no-install-recommends \
  "frr=${FRR_DEB_VERSION}" \
  "frr-pythontools=${FRR_DEB_VERSION}"
chroot "${ROOTFS}" apt-mark hold frr frr-pythontools
chroot "${ROOTFS}" apt-get clean
rm -f "${ROOTFS}/usr/sbin/policy-rc.d"
rm -rf "${ROOTFS}/var/lib/apt/lists/"*

install -d -m 0755 "${ROOTFS}/usr/share/dnlab-frr" "${ROOTFS}/usr/local/sbin"
install -m 0640 "${OUT_DIR}/daemons" "${ROOTFS}/usr/share/dnlab-frr/daemons"
install -m 0640 "${OUT_DIR}/frr.conf" "${ROOTFS}/usr/share/dnlab-frr/frr.conf"

cat > "${ROOTFS}/usr/local/sbin/dnlab-frr-guest-prepare" <<'EOF'
#!/bin/bash
set -euo pipefail

CFG_MOUNT=/run/dnlab-cfg
PERSIST_TAG=dnlab_frr_persist
CFG_TAG=dnlab_frr_cfg
MGMT_VRF_NAME=${DNLAB_MGMT_VRF_NAME:-mgmt}
MGMT_VRF_TABLE=${DNLAB_MGMT_VRF_TABLE:-1001}

mount_9p() {
  local tag=$1
  local target=$2

  mkdir -p "$target"
  if mountpoint -q "$target"; then
    return 0
  fi

  modprobe 9pnet_virtio 2>/dev/null || true
  mount -t 9p -o trans=virtio,version=9p2000.L,msize=262144 "$tag" "$target" 2>/dev/null || true
}

mount_9p "$CFG_TAG" "$CFG_MOUNT"

if [[ -f "$CFG_MOUNT/mgmt.env" ]]; then
  # shellcheck disable=SC1091
  source "$CFG_MOUNT/mgmt.env"
fi

if [[ -n "${DNLAB_HOSTNAME:-}" ]]; then
  hostnamectl set-hostname "$DNLAB_HOSTNAME" 2>/dev/null || hostname "$DNLAB_HOSTNAME"
fi

install -d -o frr -g frr -m 0750 /var/run/frr /var/log/frr

if mount_9p "$PERSIST_TAG" /etc/frr && mountpoint -q /etc/frr; then
  :
fi

install -d -o frr -g frr -m 0750 /etc/frr
for name in daemons frr.conf vtysh.conf; do
  if [[ ! -e "/etc/frr/$name" ]]; then
    if [[ -e "/usr/share/dnlab-frr/$name" ]]; then
      install -o frr -g frr -m 0640 "/usr/share/dnlab-frr/$name" "/etc/frr/$name"
    else
      install -o frr -g frr -m 0640 /dev/null "/etc/frr/$name"
    fi
  fi
done

ensure_daemon_setting() {
  local daemon=$1
  local value=$2
  local file=/etc/frr/daemons

  if grep -Eq "^[[:space:]]*${daemon}=" "$file"; then
    sed -i -E "s|^[[:space:]]*${daemon}=.*|${daemon}=${value}|" "$file"
  else
    printf '%s=%s\n' "$daemon" "$value" >> "$file"
  fi
}

ensure_daemon_setting zebra yes
ensure_daemon_setting staticd yes
ensure_daemon_setting mgmtd yes

chown -R frr:frr /etc/frr /var/run/frr /var/log/frr 2>/dev/null || true
chmod 0640 /etc/frr/daemons /etc/frr/frr.conf /etc/frr/vtysh.conf 2>/dev/null || true

sysctl -qw net.ipv4.ip_forward=1 2>/dev/null || true
sysctl -qw net.ipv4.conf.all.forwarding=1 2>/dev/null || true
sysctl -qw net.ipv4.conf.default.forwarding=1 2>/dev/null || true
sysctl -qw net.ipv4.tcp_l3mdev_accept=1 2>/dev/null || true
sysctl -qw net.ipv6.conf.all.forwarding=1 2>/dev/null || true
sysctl -qw net.ipv6.conf.default.forwarding=1 2>/dev/null || true
sysctl -qw net.ipv6.conf.all.keep_addr_on_down=1 2>/dev/null || true
sysctl -qw net.ipv6.route.skip_notify_on_dev_down=1 2>/dev/null || true

for iface in /proc/sys/net/ipv4/conf/*/forwarding; do
  [[ -e "$iface" ]] && printf '1' > "$iface" 2>/dev/null || true
done
for iface in /proc/sys/net/ipv6/conf/*/forwarding; do
  [[ -e "$iface" ]] && printf '1' > "$iface" 2>/dev/null || true
done

if ip link show dev eth0 >/dev/null 2>&1; then
  ip link set dev eth0 up || true

  if ! ip link show dev "$MGMT_VRF_NAME" >/dev/null 2>&1; then
    ip link add "$MGMT_VRF_NAME" type vrf table "$MGMT_VRF_TABLE" 2>/dev/null || true
  fi
  ip link set dev "$MGMT_VRF_NAME" up 2>/dev/null || true

  if [[ "$(basename "$(readlink -f /sys/class/net/eth0/master 2>/dev/null || true)")" != "$MGMT_VRF_NAME" ]]; then
    ip link set dev eth0 master "$MGMT_VRF_NAME" 2>/dev/null || true
  fi

  ip -4 addr flush dev eth0 2>/dev/null || true
  ip -6 addr flush dev eth0 scope global 2>/dev/null || true

  if [[ -n "${DNLAB_MGMT_IPV4:-}" && "${DNLAB_MGMT_IPV4}" != "dhcp" ]]; then
    ip -4 addr add "$DNLAB_MGMT_IPV4" dev eth0 2>/dev/null || true
  fi
  if [[ -n "${DNLAB_MGMT_IPV6:-}" && "${DNLAB_MGMT_IPV6}" != "None" && "${DNLAB_MGMT_IPV6}" != "dhcp" ]]; then
    ip -6 addr add "$DNLAB_MGMT_IPV6" dev eth0 2>/dev/null || true
  fi

  if [[ -n "${DNLAB_MGMT_GW4:-}" && "${DNLAB_MGMT_GW4}" != "dhcp" ]]; then
    ip -4 route replace vrf "$MGMT_VRF_NAME" default via "$DNLAB_MGMT_GW4" dev eth0 2>/dev/null || true
  fi
  if [[ -n "${DNLAB_MGMT_GW6:-}" && "${DNLAB_MGMT_GW6}" != "None" && "${DNLAB_MGMT_GW6}" != "dhcp" ]]; then
    ip -6 route replace vrf "$MGMT_VRF_NAME" default via "$DNLAB_MGMT_GW6" dev eth0 2>/dev/null || true
  fi
fi

exit 0
EOF
chmod 0755 "${ROOTFS}/usr/local/sbin/dnlab-frr-guest-prepare"

cat > "${ROOTFS}/usr/local/sbin/dnlab-frr-console" <<'EOF'
#!/bin/bash
while true; do
  if /usr/bin/vtysh; then
    exit 0
  fi
  echo "Waiting for FRR..."
  sleep 2
done
EOF
chmod 0755 "${ROOTFS}/usr/local/sbin/dnlab-frr-console"

cat > "${ROOTFS}/etc/systemd/system/dnlab-frr-prepare.service" <<'EOF'
[Unit]
Description=DNLab FRR guest preparation
DefaultDependencies=no
After=local-fs.target systemd-modules-load.service
Before=network-pre.target frr.service serial-getty@ttyS0.service
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/dnlab-frr-guest-prepare
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "${ROOTFS}/etc/systemd/system/serial-getty@ttyS0.service.d"
cat > "${ROOTFS}/etc/systemd/system/serial-getty@ttyS0.service.d/override.conf" <<'EOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin frrvty --noclear %I linux
EOF

chroot "${ROOTFS}" useradd -r -m -g frrvty -G frr -s /usr/local/sbin/dnlab-frr-console frrvty
chroot "${ROOTFS}" systemctl enable dnlab-frr-prepare.service frr.service serial-getty@ttyS0.service
ln -sf /lib/systemd/systemd "${ROOTFS}/sbin/init"

cat > "${ROOTFS}/etc/fstab" <<'EOF'
/dev/vda1 / ext4 defaults 0 1
EOF

echo "hosts: files dns" > "${ROOTFS}/etc/nsswitch.conf"
echo "dnlab-frr" > "${ROOTFS}/etc/hostname"

kernel=$(find "${ROOTFS}/boot" -maxdepth 1 -name 'vmlinuz-*' | sort -V | tail -n 1)
initrd=$(find "${ROOTFS}/boot" -maxdepth 1 -name 'initrd.img-*' | sort -V | tail -n 1)
install -m 0644 "$kernel" "${OUT_DIR}/vmlinuz"
install -m 0644 "$initrd" "${OUT_DIR}/initrd.img"

truncate -s "${DISK_SIZE_MB}M" "${RAW_DISK}"
printf ',,L,*\n' | sfdisk "${RAW_DISK}"
mkfs.ext4 -F -L rootfs -d "${ROOTFS}" "${ROOTFS_IMG}" "${ROOTFS_SIZE_MB}M"
dd if="${ROOTFS_IMG}" of="${RAW_DISK}" bs=512 seek=2048 conv=notrunc status=none
qemu-img convert -f raw -O qcow2 "${RAW_DISK}" "${OUT_DIR}/installed.qcow2"

printf '%s:%s:%s' "${VERSION}" "${DISK_SIZE_MB}" "${BUILD_REVISION}" > "${MARKER}"
rm -rf "${BUILD_DIR}"

echo "Built ${OUT_DIR}/installed.qcow2 for FRR ${VERSION}"
