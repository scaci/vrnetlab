#!/usr/bin/env python3

import datetime
import logging
import os
import re
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

DEFAULT_RAM_MB = 2048
DEFAULT_SMP = "2"
BOOT_SPIN_LIMIT = 6000
PERSIST_DIRS = ("/persist", "/config")


class CumulusVX_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, nics, conn_mode):
        disk_image = self._find_disk_image()

        super(CumulusVX_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=DEFAULT_RAM_MB,
            smp=DEFAULT_SMP,
            mgmt_passthrough=True,
            mgmt_dhcp=True,
        )

        self.hostname = hostname
        self.num_nics = nics
        self.conn_mode = conn_mode
        self.nic_type = "virtio-net-pci"
        self._enable_persistent_overlay(disk_image)

    @staticmethod
    def _find_disk_image():
        for entry in os.listdir("/"):
            if re.search(r"(cumulus.*|.*cumulus.*)\.qcow2$", entry, re.IGNORECASE):
                return "/" + entry

        for entry in os.listdir("/"):
            if entry.endswith(".qcow2"):
                return "/" + entry

        raise RuntimeError(
            "No Cumulus VX qcow2 image found. Copy the qcow2 into the build "
            "directory before running make."
        )

    def _enable_persistent_overlay(self, disk_image):
        # dnlab-patched: cumulus-vx-persist-dir-v1
        persist_dir = next((path for path in PERSIST_DIRS if os.path.isdir(path)), None)
        if not persist_dir:
            self.logger.warning(
                "No persistence mount found; Cumulus VX disk changes are ephemeral"
            )
            return

        persistent_overlay = os.path.join(persist_dir, "cumulusvx_overlay.qcow2")
        if not os.path.exists(persistent_overlay):
            vrnetlab.run_command(
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-b",
                    disk_image,
                    "-F",
                    "qcow2",
                    persistent_overlay,
                ]
            )
            self.logger.info("Created persistent overlay at %s", persistent_overlay)
        else:
            self.logger.info("Reusing persistent overlay at %s", persistent_overlay)

        for i, arg in enumerate(self.qemu_args):
            if "file=" in arg and "-overlay" in arg:
                self.qemu_args[i] = arg.split("file=")[0] + "file=" + persistent_overlay
                self.logger.info("Patched qemu_args drive to use persistent overlay")
                break

    def bootstrap_spin(self):
        if self.spins > BOOT_SPIN_LIMIT:
            self.logger.debug("Too many spins -> restarting VM")
            self.stop()
            self.start()
            return

        (ridx, match, res) = self.tn.expect(
            [
                b"login: ",
                b"Login: ",
                b"cumulus login: ",
                b"Cumulus login: ",
            ],
            1,
        )

        if match:
            self.logger.debug("Cumulus VX login prompt detected")
            self.running = True
            self.tn.close()
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info("Startup complete in: %s", startup_time)
            return

        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode(errors="ignore"))
            self.spins = 0

        self.spins += 1


class CumulusVX(vrnetlab.VR):
    def __init__(self, hostname, username, password, nics, conn_mode):
        super(CumulusVX, self).__init__(username, password)
        self.vms = [CumulusVX_vm(hostname, username, password, nics, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NVIDIA Cumulus VX launcher")
    parser.add_argument("--trace", action="store_true", help="Enable trace logging")
    parser.add_argument("--username", default="cumulus", help="Username")
    parser.add_argument("--password", default="CumulusLinux!", help="Password")
    parser.add_argument("--hostname", default="cumulusvx", help="VM hostname")
    parser.add_argument("--nics", type=int, default=32, help="Number of data NICs")
    parser.add_argument(
        "--connection-mode",
        default="tc",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s %(name)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = CumulusVX(
        args.hostname,
        args.username,
        args.password,
        args.nics,
        args.connection_mode,
    )
    vr.start()
