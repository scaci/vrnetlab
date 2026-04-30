#!/usr/bin/env python3

import datetime
import logging
import math
import os
import random
import re
import signal
import string
import subprocess
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
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


def env_int(names, default):
    """Return the first integer value found in the supplied environment names."""
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        match = re.search(r"\d+", str(value))
        if match:
            return int(match.group(0))
        logging.getLogger().warning(
            "Ignoring %s=%r because it does not contain an integer", name, value
        )
    return default


class cat9kv_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode, vcpu, ram):
        disk_image = None
        for e in sorted(os.listdir("/")):
            if not disk_image and re.search(".qcow2$", e):
                disk_image = "/" + e
        self.is_c9800 = bool(disk_image and re.search(r"c9800", disk_image, re.IGNORECASE))
        min_dp_nics = 2 if self.is_c9800 else 8

        super().__init__(
            username,
            password,
            disk_image=disk_image,
            smp=f"cores={vcpu},threads=1,sockets=1",
            ram=ram,
            min_dp_nics=min_dp_nics,
            use_scrapli=True,
        )
        self.hostname = hostname
        self.conn_mode = conn_mode
        self.num_nics = 3 if self.is_c9800 else 9
        self.nic_type = "virtio-net-pci"

        self.image_name = "config.img"
        self.has_boot_image = (
            not self.is_c9800
            or os.path.exists(STARTUP_CONFIG_FILE)
            or os.path.exists("/vswitch.xml")
        )

        self.qemu_args.extend(["-overcommit mem-lock=off"])
        if self.has_boot_image:
            self.qemu_args.extend([f"-boot order=cd -cdrom /{self.image_name}"])
            # create .img which is mounted for optional startup config and ASIC emulation in 'conf/vswitch.xml' dir.
            self.create_boot_image()
        else:
            self.logger.info("C9800 image without startup config; not attaching bootstrap ISO")

    def create_boot_image(self):
        """Creates an ISO image with optional boot-time files."""
        try:
            os.makedirs("/img_dir/conf")
        except FileExistsError:
            pass
        except Exception as exc:
            self.logger.error("Unable to make '/img_dir/conf': %s", exc)

        try:
            # Load vswitch.xml and preserve the serial number provided by the GUI.
            if os.path.exists("/vswitch.xml"):
                with open("/vswitch.xml", "r") as f:
                    vswitch_content = f.read()

                serial_match = re.search(
                    r"<prod_serial_number>(.*?)</prod_serial_number>",
                    vswitch_content,
                    re.DOTALL,
                ) or re.search(
                    r"<serial_number>(.*?)</serial_number>",
                    vswitch_content,
                    re.DOTALL,
                )
                serial_number = ""
                if serial_match:
                    serial_number = re.sub(r"[^A-Za-z0-9]", "", serial_match.group(1)).upper()
                if not serial_number:
                    serial_number = ''.join(random.choices(string.ascii_uppercase + string.digits, k=11))

                if re.search(r"<prod_serial_number>.*?</prod_serial_number>", vswitch_content, re.DOTALL):
                    vswitch_content = re.sub(
                        r"<prod_serial_number>.*?</prod_serial_number>",
                        f"<prod_serial_number>{serial_number}</prod_serial_number>",
                        vswitch_content,
                        flags=re.DOTALL,
                    )
                else:
                    vswitch_content = re.sub(
                        r"</vswitch>",
                        f"  <prod_serial_number>{serial_number}</prod_serial_number>\n</vswitch>",
                        vswitch_content,
                        count=1,
                    )

                with open("/img_dir/conf/vswitch.xml", "w") as f:
                    f.write(vswitch_content)

                self.logger.info("Generated vswitch.xml with serial number: %s", serial_number)
            else:
                self.logger.debug("No vswitch.xml file provided.")
        except Exception as e:
            self.logger.error(f"Error processing vswitch.xml: {e}")

        if os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.info("Startup configuration file found; using it without autogenerated defaults")
            with open(STARTUP_CONFIG_FILE, "r") as startup_config:
                cat9kv_config = startup_config.read()

            with open("/img_dir/iosxe_config.txt", "w") as cfg_file:
                cfg_file.write(cat9kv_config)
        else:
            self.logger.info("No startup configuration file found; booting with factory-default configuration")

        genisoimage_args = [
            "genisoimage",
            "-l",
            "-o",
            "/" + self.image_name,
            "/img_dir",
        ]

        self.logger.debug("Generating boot ISO")
        subprocess.Popen(genisoimage_args).wait()

    def create_dummy_tap_ifup(self):
        """Create a tap ifup script for dummy NICs required during boot."""
        ifup_script = """#!/bin/bash

TAP_IF=$1

ip link set $TAP_IF up
ip link set $TAP_IF mtu 65000 || true

# disable IPv6 to avoid periodic traffic from the vrnetlab container
ip -6 addr flush $TAP_IF || true
"""

        with open("/etc/dummy-tap-ifup", "w") as f:
            f.write(ifup_script)
        os.chmod("/etc/dummy-tap-ifup", 0o777)

    def gen_dummy_nics(self):
        """Generate link-up dummy NICs so IOS-XE images see all required ports."""
        if not self.is_c9800:
            return super().gen_dummy_nics()

        nics = self.min_nics - self.num_provisioned_nics

        self.logger.debug("Insufficient NICs defined. Generating %s dummy nics", nics)
        self.create_dummy_tap_ifup()

        res = []
        pci_bus_ctr = self.num_provisioned_nics

        for i in range(0, nics):
            interface_name = f"dummy{str(i + self.num_provisioned_nics)}"
            pci_bus_ctr += 1

            pci_bus = math.floor(pci_bus_ctr / self.nics_per_pci_bus) + 1
            addr = (pci_bus_ctr % self.nics_per_pci_bus) + 1

            res.extend(
                [
                    "-device",
                    f"{self.nic_type},netdev={interface_name},id={interface_name},mac={vrnetlab.gen_mac(i)},bus=pci.{pci_bus},addr=0x{addr}",
                    "-netdev",
                    f"tap,ifname={interface_name},id={interface_name},script=/etc/dummy-tap-ifup,downscript=no",
                ]
            )
        return res

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        boot_patterns = [
            b"CVAC-4-CONFIG_DONE",
            b"IOSXEBOOT-4-FACTORY_RESET",
            b"Would you like to enter the initial configuration dialog",
            b"Press RETURN to get started",
            b"No startup-config, starting autoinstall/pnp/ztp",
            b"Autoinstall will terminate if any input is detected on console",
            b"Enter Administrative User Name",
        ]
        (ridx, match, res) = self.con_expect(boot_patterns)
        if match:  # got a match!
            if ridx == 0:  # configuration applied
                self.logger.info("CVAC Configuration has been applied.")
                self._mark_running()
                return
            elif ridx == 1:  # IOSXEBOOT-4-FACTORY_RESET
                self.logger.warning("Unexpected reload while running")
            elif ridx in [2, 3, 4, 5, 6]:  # no boot config was injected
                self.logger.info("Startup complete with factory-default configuration.")
                self._mark_running()
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.write_to_stdout(res)
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def _mark_running(self):
        self.scrapli_tn.close()
        startup_time = datetime.datetime.now() - self.start_time
        self.logger.info("Startup complete in: %s", startup_time)
        self.running = True


class cat9kv(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode, vcpu, ram):
        super(cat9kv, self).__init__(username, password)
        self.vms = [cat9kv_vm(hostname, username, password, conn_mode, vcpu, ram)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument("--hostname", default="", help="Router hostname")
    parser.add_argument(
        "--connection-mode",
        default="vrxcon",
        help="Connection mode to use in the datapath",
    )
    parser.add_argument("--vcpu", type=int, default=None, help="Allocated vCPUs")
    parser.add_argument("--ram", type=int, default=None, help="Allocated RAM in MB")

    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    # Auto-detect hostname from image filename if not provided
    if not args.hostname:
        for e in os.listdir("/"):
            if re.search(r"\.qcow2$", e):
                if re.search(r"c9800", e, re.IGNORECASE):
                    args.hostname = "c9800cl"
                else:
                    args.hostname = "cat9kv"
                break
        if not args.hostname:
            args.hostname = "cat9kv"

    args.vcpu = args.vcpu or env_int(
        [
            "VCPU",
            "CPU",
            "QEMU_VCPU",
            "QEMU_SMP",
            "CLAB_LABEL_VCPU",
            "CLAB_LABEL_CPU",
            "CLAB_LABEL_NODE_VCPU",
            "CLAB_LABEL_NODE_CPU",
        ],
        4,
    )
    args.ram = args.ram or env_int(
        [
            "RAM",
            "MEMORY",
            "QEMU_MEMORY",
            "CLAB_LABEL_RAM",
            "CLAB_LABEL_MEMORY",
            "CLAB_LABEL_NODE_RAM",
            "CLAB_LABEL_NODE_MEMORY",
        ],
        18432,
    )

    vr = cat9kv(
        args.hostname,
        args.username,
        args.password,
        args.connection_mode,
        args.vcpu,
        args.ram,
    )
    vr.start()
