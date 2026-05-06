# DNLab FRR

Native FRRouting container image built with a vrnetlab-style target layout.

The image is based on Debian 13 slim and installs FRR from the official
FRRouting Debian repository. The default tag is:

```text
vrnetlab/dnlab_frr:10.6.1-dnlab
```

Build:

```bash
make
```

Default daemons enabled in `/etc/frr/daemons`:

```text
zebra=yes
staticd=yes
mgmtd=yes
```

Protocol daemons are intentionally disabled by default and are expected to be
enabled per node by DNLab GUI node features.

The image is DNLab-native: when `/persist` is mounted, FRR configuration files
under `/etc/frr` are symlinked to `/persist/frr`.

In containerlab this image must be deployed as `kind: linux`; `eth0` remains
the management interface managed by containerlab's management network.
