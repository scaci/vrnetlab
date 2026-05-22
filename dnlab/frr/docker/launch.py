#!/usr/bin/env python3

import datetime
import logging
import os
import shlex
import signal
import sys

import vrnetlab


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace

CONFIG_SHARE = "/run/dnlab-frr"
CONFIG_ENV = os.path.join(CONFIG_SHARE, "mgmt.env")


def _resolve_disk_image() -> str:
    base = "/installed.qcow2"
    persist_link = "/persist/base.qcow2"
    if os.path.isdir("/persist"):
        if not os.path.lexists(persist_link):
            os.symlink(base, persist_link)
        return persist_link
    return base


def _quote_env(value) -> str:
    if value is None:
        value = "None"
    return shlex.quote(str(value))


class DNLabFRRVM(vrnetlab.VM):
    def __init__(self, hostname, username, password, nics, conn_mode):
        disk_image = _resolve_disk_image()
        super().__init__(
            username,
            password,
            disk_image=disk_image,
            ram=512,
            driveif="virtio",
            mgmt_passthrough=True,
        )

        self.hostname = hostname
        self.conn_mode = conn_mode
        self.nic_type = "virtio-net-pci"
        self.num_nics = nics
        self.mgmt_tcp_ports = []

        self._write_config_share()
        self.qemu_args.extend(
            [
                "-kernel",
                "/vmlinuz",
                "-initrd",
                "/initrd.img",
                "-append",
                '"root=/dev/vda1 rw console=ttyS0 net.ifnames=0 systemd.show_status=false"',
                "-virtfs",
                f"local,path={CONFIG_SHARE},mount_tag=dnlab_frr_cfg,security_model=none,id=dnlab_frr_cfg",
            ]
        )

        if os.path.isdir("/persist"):
            os.makedirs("/persist/frr", exist_ok=True)
            self.qemu_args.extend(
                [
                    "-virtfs",
                    "local,path=/persist/frr,mount_tag=dnlab_frr_persist,security_model=none,id=dnlab_frr_persist",
                ]
            )

    def _write_config_share(self):
        os.makedirs(CONFIG_SHARE, exist_ok=True)
        with open(CONFIG_ENV, "w") as f:
            f.write(f"DNLAB_HOSTNAME={_quote_env(self.hostname)}\n")
            f.write(f"DNLAB_MGMT_IPV4={_quote_env(self.mgmt_address_ipv4)}\n")
            f.write(f"DNLAB_MGMT_IPV6={_quote_env(self.mgmt_address_ipv6)}\n")
            f.write(f"DNLAB_MGMT_GW4={_quote_env(self.mgmt_gw_ipv4)}\n")
            f.write(f"DNLAB_MGMT_GW6={_quote_env(self.mgmt_gw_ipv6)}\n")

    def bootstrap_spin(self):
        if self.spins > 600:
            self.stop()
            self.start()
            return

        patterns = [b"frr#", b"Waiting for FRR", b"login:"]
        (ridx, match, res) = self.tn.expect(patterns, 1)
        if match:
            self.logger.debug("matched boot pattern: %r", patterns[ridx])
            if ridx == 2:
                self.tn.write(b"\r")

            self.running = True
            self.tn.close()
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info("Startup complete in: %s", startup_time)
            return

        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode(errors="replace"))
            self.spins = 0

        self.spins += 1

    def gen_mgmt(self):
        res = super().gen_mgmt()
        if self.num_nics > 0 and "bus=pci.1" not in res[-3]:
            res[-3] = res[-3] + ",bus=pci.1"
        return res


class DNLabFRR(vrnetlab.VR):
    def __init__(self, hostname, username, password, nics, conn_mode):
        super().__init__(username, password, mgmt_passthrough=True)
        self.vms = [DNLabFRRVM(hostname, username, password, nics, conn_mode)]


if __name__ == "__main__":
    import argparse

    for src, dst in (("RAM", "QEMU_MEMORY"), ("VCPU", "QEMU_SMP")):
        if src in os.environ and dst not in os.environ:
            os.environ[dst] = os.environ[src]

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--username", default="frr", help="Username")
    parser.add_argument("--password", default="frr", help="Password")
    parser.add_argument("--hostname", default="frr", help="VM hostname")
    parser.add_argument("--nics", type=int, default=16, help="Number of NICs")
    parser.add_argument(
        "--connection-mode",
        default="tc",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = DNLabFRR(
        args.hostname,
        args.username,
        args.password,
        args.nics,
        args.connection_mode,
    )
    vr.start()
