#!/bin/bash
set -euo pipefail

install -d -o frr -g frr -m 0750 /var/run/frr /var/log/frr

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

    rm -f "$src"
    ln -s "$dst" "$src"
  done

  chown -R frr:frr "$persist_frr" 2>/dev/null || true
  chmod 0640 "$persist_frr"/daemons "$persist_frr"/frr.conf "$persist_frr"/vtysh.conf 2>/dev/null || true
}

dnlab_prepare_persistent_frr

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
