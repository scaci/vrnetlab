# DNLab FRR

FRRouting VM-in-container image built with a vrnetlab-style target layout.

The runtime container is based on Debian 13 slim and starts a QEMU VM that runs
Debian 13 with FRR from the official FRRouting Debian repository. The default
tag is:

```text
vrnetlab/dnlab_frr:10.6.1-dnlab
```

Build:

```bash
make
```

The build generates `docker/installed.qcow2`, `docker/vmlinuz`, and
`docker/initrd.img` automatically; no external FRR qcow2 is required.

Default daemons enabled in `/etc/frr/daemons`:

```text
zebra=yes
staticd=yes
mgmtd=yes
```

Protocol daemons are intentionally disabled by default and are expected to be
enabled per node by DNLab GUI node features.

When `/persist` is mounted, `/persist/frr` is exposed to the guest VM over 9p
and mounted over `/etc/frr`. This keeps FRR's atomic configuration writes inside
the persistent directory.

The image preserves the DNLab GUI-facing deployment contract. The VM uses
transparent management passthrough, so guest `eth0` receives the container
management address, and dataplane interfaces remain `eth1+`. The serial console
is exposed on TCP/5000 and lands in `vtysh`.
