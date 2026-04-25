#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import sys

import vrnetlab

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"


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


class N9KV_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        disk_image = ""
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
        if disk_image == "":
            logging.getLogger().info("Disk image was not found")
            exit(1)
        super(N9KV_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=10240,
            smp=4,
            cpu="host",
            use_scrapli=True,
        )
        self.hostname = hostname
        self.conn_mode = conn_mode
        self.num_nics = 129
        self.nic_type = "e1000"

        self.qemu_args.extend(["-bios", "/OVMF.fd"])

        overlay_disk_image = re.sub(r"(\.[^.]+$)", r"-overlay\1", disk_image)
        self.qemu_args.extend(["-boot", "c"])
        replace_index = self.qemu_args.index(
            "if=ide,file={}".format(overlay_disk_image)
        )
        self.qemu_args[replace_index] = (
            "file={},if=none,id=drive-sata-disk0,format=qcow2".format(
                overlay_disk_image
            )
        )
        self.qemu_args.extend(["-device", "ahci,id=ahci0,bus=pci.0"])
        self.qemu_args.extend(
            [
                "-device",
                "ide-hd,drive=drive-sata-disk0,bus=ahci0.0,id=drive-sata-disk0,bootindex=1",
            ]
        )

    def bootstrap_spin(self):
        """
        Waits for VDC_ONLINE or POAP syslog messages, then releases the
        serial console (port 5000) so the user can connect and interact
        with the POAP prompt directly.
        """
        if self.spins > 300:
            self.stop()
            self.start()
            return

        (ridx, match, res) = self.con_expect(
            [
                b"VDC_MGR-2-VDC_ONLINE",
                b"POAP-2-POAP_INITED",
                b"POAP-2-POAP_DISABLED",
            ]
        )

        if match:
            startup_time = datetime.datetime.now() - self.start_time
            self.logger.info(
                "System ready in %s (ridx=%d) - releasing console on port 5000"
                % (startup_time, ridx)
            )
            # Chiude la connessione scrapli così la porta 5000 è libera per l'utente
            try:
                self.scrapli_tn.close()
                self.logger.info("Serial console released")
            except Exception as e:
                self.logger.warning("Could not close scrapli_tn: %s" % e)

            self.running = True
            return

        if res != b"":
            self.write_to_stdout(res)
            self.spins = 0

        self.spins += 1


class N9KV(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(N9KV, self).__init__(username, password)
        self.vms = [N9KV_vm(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="vr-n9kv", help="Router hostname")
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument(
        "--connection-mode",
        default="vrxcon",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vrnetlab.boot_delay()

    vr = N9KV(args.hostname, args.username, args.password, args.connection_mode)
    vr.start()