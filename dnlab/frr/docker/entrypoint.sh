#!/bin/bash
set -euo pipefail

install -d -o frr -g frr -m 0750 /var/run/frr /var/log/frr
rm -rf /var/tmp/frr/watchfrr.* 2>/dev/null || true

dnlab_prepare_mgmt_vrf() {
  local vrf_name=${DNLAB_MGMT_VRF_NAME:-mgmt}
  local table_id=${DNLAB_MGMT_VRF_TABLE:-1001}

  if ! ip link show dev eth0 >/dev/null 2>&1; then
    return
  fi

  local ipv4_defaults=()
  local ipv6_defaults=()
  mapfile -t ipv4_defaults < <(ip -4 route show default dev eth0 2>/dev/null || true)
  mapfile -t ipv6_defaults < <(ip -6 route show default dev eth0 2>/dev/null || true)

  if ! ip link show dev "$vrf_name" >/dev/null 2>&1; then
    ip link add "$vrf_name" type vrf table "$table_id"
  fi
  ip link set dev "$vrf_name" up

  local route
  for route in "${ipv4_defaults[@]}"; do
    [[ -n "$route" ]] && ip -4 route del $route 2>/dev/null || true
  done
  for route in "${ipv6_defaults[@]}"; do
    [[ -n "$route" ]] && ip -6 route del $route 2>/dev/null || true
  done

  if [[ "$(basename "$(readlink -f /sys/class/net/eth0/master 2>/dev/null || true)")" != "$vrf_name" ]]; then
    ip link set dev eth0 master "$vrf_name"
  fi
  ip link set dev eth0 up

  for route in "${ipv4_defaults[@]}"; do
    [[ -n "$route" ]] && ip -4 route replace vrf "$vrf_name" $route 2>/dev/null || true
  done
  for route in "${ipv6_defaults[@]}"; do
    [[ -n "$route" ]] && ip -6 route replace vrf "$vrf_name" $route 2>/dev/null || true
  done
}

dnlab_prepare_mgmt_vrf

dnlab_apply_router_sysctls() {
  local keys=(
    net.ipv4.ip_forward=1
    net.ipv4.conf.all.forwarding=1
    net.ipv4.conf.default.forwarding=1
    net.ipv6.conf.all.forwarding=1
    net.ipv6.conf.default.forwarding=1
    net.ipv6.conf.all.keep_addr_on_down=1
    net.ipv6.route.skip_notify_on_dev_down=1
  )

  local item
  for item in "${keys[@]}"; do
    sysctl -qw "$item" 2>/dev/null || true
  done

  local iface
  for iface in /proc/sys/net/ipv4/conf/*/forwarding; do
    [[ -e "$iface" ]] && printf '1' > "$iface" 2>/dev/null || true
  done
  for iface in /proc/sys/net/ipv6/conf/*/forwarding; do
    [[ -e "$iface" ]] && printf '1' > "$iface" 2>/dev/null || true
  done
}

dnlab_apply_router_sysctls

dnlab_prepare_persistent_frr() {
  if [[ ! -d /persist ]]; then
    return
  fi

  local persist_frr=/persist/frr
  install -d -o frr -g frr -m 0750 "$persist_frr"

  local name src dst
  for name in daemons frr.conf vtysh.conf; do
    src="/etc/frr/$name"
    dst="$persist_frr/$name"

    if [[ ! -e "$dst" ]]; then
      if [[ -e "$src" && ! -L "$src" ]]; then
        cp -a "$src" "$dst"
      else
        : > "$dst"
      fi
    fi
  done

  if ! awk '$5 == "/etc/frr" { found = 1 } END { exit !found }' /proc/self/mountinfo; then
    mount --bind "$persist_frr" /etc/frr
  fi

  chown -R frr:frr "$persist_frr" 2>/dev/null || true
  chmod 0640 "$persist_frr"/daemons "$persist_frr"/frr.conf "$persist_frr"/vtysh.conf 2>/dev/null || true
}

dnlab_prepare_persistent_frr

dnlab_ensure_daemon_setting() {
  local daemon=$1
  local value=$2
  local file=/etc/frr/daemons

  if grep -Eq "^[[:space:]]*${daemon}=" "$file"; then
    sed -i -E "s|^[[:space:]]*${daemon}=.*|${daemon}=${value}|" "$file"
  else
    printf '%s=%s\n' "$daemon" "$value" >> "$file"
  fi
}

dnlab_normalize_daemons() {
  local file=/etc/frr/daemons
  [[ -f "$file" ]] || : > "$file"

  dnlab_ensure_daemon_setting zebra yes
  dnlab_ensure_daemon_setting staticd yes
  dnlab_ensure_daemon_setting mgmtd yes
}

dnlab_normalize_daemons

if [[ -f /etc/frr/daemons ]]; then
  chown frr:frr /etc/frr/daemons
  chmod 0640 /etc/frr/daemons
fi

if [[ -f /etc/frr/frr.conf ]]; then
  chown frr:frr /etc/frr/frr.conf
  chmod 0640 /etc/frr/frr.conf
fi

if [[ "$#" -eq 0 ]]; then
  daemons=()
  for daemon in \
    zebra bgpd ospfd ospf6d ripd ripngd isisd pimd pim6d ldpd nhrpd \
    eigrpd babeld sharpd pbrd bfdd fabricd vrrpd pathd staticd mgmtd
  do
    if grep -Eq "^[[:space:]]*${daemon}=yes([[:space:]]|$)" /etc/frr/daemons; then
      daemons+=("$daemon")
    fi
  done
  set -- /usr/lib/frr/watchfrr -F traditional "${daemons[@]}"
fi

exec "$@"
