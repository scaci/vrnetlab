#!/usr/bin/env python3
import datetime
import ipaddress
import logging
import os
import re
import signal
import subprocess
import socket
import struct
import sys
import threading
import time
import uuid

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
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class VJUNOSSWITCH_vm(vrnetlab.VM):
    def __init__(self, hostname, username, password, conn_mode):
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e
        ram = max(512, int(os.getenv("RAM", "5120")))
        vcpu = max(1, int(os.getenv("VCPU", "4")))
        super(VJUNOSSWITCH_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=ram,
            driveif="virtio",
            cpu="host",
            smp=f"{vcpu},sockets=1,cores={vcpu},threads=1",
            mgmt_passthrough=True,
        )
        # device hostname
        self.hostname = hostname

        with open("init.conf", "r") as file:
            cfg = file.read()

        cfg = cfg.replace("{MGMT_IP_IPV4}", str(self.mgmt_address_ipv4 or ""))
        cfg = cfg.replace("{MGMT_GW_IPV4}", str(self.mgmt_gw_ipv4 or ""))
        if self.mgmt_address_ipv6 and self.mgmt_gw_ipv6:
            cfg = cfg.replace("{MGMT_IP_IPV6}", str(self.mgmt_address_ipv6))
            cfg = cfg.replace("{MGMT_GW_IPV6}", str(self.mgmt_gw_ipv6))
        else:
            cfg = re.sub(
                r"\n\s*family inet6 \{\n\s*address \{MGMT_IP_IPV6\};\n\s*\}",
                "",
                cfg,
            )
            cfg = re.sub(
                r"\n\s*rib mgmt_junos\.inet6\.0 \{\n\s*static \{\n\s*route ::/0 next-hop \{MGMT_GW_IPV6\};\n\s*\}\n\s*\}",
                "",
                cfg,
            )
        cfg = cfg.replace("{HOSTNAME}", self.hostname)

        with open("init.conf", "w") as file:
            file.write(cfg)

        self.startup_config()

        # these QEMU cmd line args are translated from the shipped libvirt XML file
        self.qemu_args.extend(["-overcommit", "mem-lock=off"])
        # generate UUID to attach
        self.qemu_args.extend(["-uuid", str(uuid.uuid4())])

        # extend QEMU args with device USB details, xhci is most virtualization-friendly
        self.qemu_args.extend(["-device", "qemu-xhci,id=usb,bus=pci.0,addr=0x1.0x2"])


        # vJunos requires a metadata disk with the bootstrap config.
        self.qemu_args.extend(
            [
                "-drive",
                "file=/config.img,format=raw,if=none,id=config_disk",
                "-device",
                "usb-storage,drive=config_disk,id=usb-disk0,removable=off,write-cache=on",
            ]
        )

        self.qemu_args.extend(["-no-user-config", "-nodefaults", "-boot", "strict=on"])
        self.nic_type = "virtio-net-pci"
        # 1 management port + 48 front ports + 8 uplink ports to match most dense 1U Juniper switch
        self.num_nics = 57
        self.smbios = ["type=1,product=VM-VEX"]
        self.conn_mode = conn_mode


    def startup_config(self):
        """Create the vJunos metadata disk from init.conf and optional user config."""
        startup_config_file = "/config/startup-config.cfg"
        if not os.path.exists(startup_config_file):
            self.logger.trace(f"Startup config file {startup_config_file} is not found")
            os.rename("init.conf", "juniper.conf")
        else:
            self.logger.trace(
                f"Startup config file {startup_config_file} found, appending initial configuration"
            )
            append_cfg = f"cat init.conf {startup_config_file} >> juniper.conf"
            subprocess.run(append_cfg, shell=True)

        subprocess.run(["./make-config.sh", "juniper.conf", "config.img"], check=True)

    def create_tc_tap_mgmt_ifup(self):
        super().create_tc_tap_mgmt_ifup()
        with open("/etc/tc-tap-mgmt-ifup", "a") as f:
            f.write(
                "\n"
                "# Keep vJunos wrapper DHCP local to the container.\n"
                "ip addr add 169.254.0.1/32 dev tap0 2>/dev/null || true\n"
                "tc filter add dev tap0 ingress prio 1 protocol ip flower "
                "ip_proto udp src_port 68 dst_port 67 action pass\n"
            )

    def start(self):
        if self.mgmt_passthrough:
            self._start_mgmt_dhcp()
        super().start()

    def _start_mgmt_dhcp(self):
        old_sock = getattr(self, "_mgmt_dhcp_sock", None)
        if old_sock is not None:
            try:
                old_sock.close()
            except Exception:
                pass
        token = object()
        self._mgmt_dhcp_token = token
        thread = threading.Thread(target=self._mgmt_dhcp_loop, args=(token,), daemon=True)
        thread.start()

    def _mgmt_dhcp_loop(self, token):
        address = (self.mgmt_address_ipv4 or "").split("/")[0]
        if not address or address == "dhcp":
            return
        gateway = (self.mgmt_gw_ipv4 or "").split("/")[0] or address
        try:
            prefix = ipaddress.IPv4Interface(self.mgmt_address_ipv4).network.prefixlen
            netmask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
        except Exception:
            netmask = "255.255.255.0"

        for _ in range(100):
            if os.path.exists("/sys/class/net/tap0"):
                break
            time.sleep(0.1)

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tap0")
            sock.bind(("", 67))
        except Exception as e:
            self.logger.warning("Could not start vJunos mgmt DHCP helper: %s" % e)
            return
        self._mgmt_dhcp_sock = sock

        self.logger.info(
            "Started vJunos mgmt DHCP helper on tap0 for %s via %s" % (address, gateway)
        )
        while getattr(self, "_mgmt_dhcp_token", None) is token:
            try:
                data, _ = sock.recvfrom(2048)
                if len(data) < 240:
                    continue
                msg_type = self._dhcp_msg_type(data)
                if msg_type not in (1, 3):
                    continue
                response_type = 2 if msg_type == 1 else 5
                sock.sendto(
                    self._dhcp_response(data, response_type, address, gateway, netmask),
                    ("255.255.255.255", 68),
                )
            except Exception as e:
                if getattr(self, "_mgmt_dhcp_token", None) is token:
                    self.logger.trace("vJunos mgmt DHCP helper error: %s" % e)
        try:
            sock.close()
        except Exception:
            pass

    def _dhcp_msg_type(self, data):
        idx = 240
        while idx < len(data):
            opt = data[idx]
            if opt == 255:
                break
            if opt == 0:
                idx += 1
                continue
            length = data[idx + 1]
            if opt == 53 and length == 1:
                return data[idx + 2]
            idx += 2 + length
        return None

    def _dhcp_response(self, request, msg_type, address, gateway, netmask):
        packet = bytearray(240)
        packet[0] = 2
        packet[1:4] = request[1:4]
        packet[4:8] = request[4:8]
        packet[10:12] = request[10:12]
        packet[16:20] = socket.inet_aton(address)
        packet[20:24] = socket.inet_aton("169.254.0.1")
        packet[28:44] = request[28:44]
        packet[236:240] = b"\x63\x82\x53\x63"
        options = b"".join(
            [
                b"\x35\x01" + bytes([msg_type]),
                b"\x36\x04" + socket.inet_aton("169.254.0.1"),
                b"\x01\x04" + socket.inet_aton(netmask),
                b"\x03\x04" + socket.inet_aton(gateway),
                b"\x06\x04" + socket.inet_aton(gateway),
                b"\x33\x04" + struct.pack("!I", 86400),
                b"\xff",
            ]
        )
        return bytes(packet) + options

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""
        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        # lets wait for the OS/platform log to determine if VM is booted,
        # login prompt can get lost in boot logs
        (ridx, match, res) = self.tn.expect([b"FreeBSD/amd64"], 1)
        if match:  # got a match!
            if ridx == 0:  # login
                self.logger.info("VM started")

                # Login
                self.wait_write("\r", None)

                _, loginMatch, _ = self.tn.expect([b"login:"], 1)
                if loginMatch:

                    self.logger.info("Login prompt found")

                    # close telnet connection
                    self.tn.close()
                    # startup time?
                    startup_time = datetime.datetime.now() - self.start_time
                    self.logger.info("Startup complete in: %s" % startup_time)
                    # mark as running
                    self.running = True
                    return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return


class VJUNOSSWITCH(vrnetlab.VR):
    def __init__(self, hostname, username, password, conn_mode):
        super(VJUNOSSWITCH, self).__init__(username, password)
        self.vms = [VJUNOSSWITCH_vm(hostname, username, password, conn_mode)]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument(
        "--hostname", default="vr-vjunosswitch", help="vJunos-switch hostname"
    )
    parser.add_argument("--username", default="vrnetlab", help="Username")
    parser.add_argument("--password", default="VR-netlab9", help="Password")
    parser.add_argument(
        "--connection-mode", default="tc", help="Connection mode to use in the datapath"
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    vr = VJUNOSSWITCH(
        args.hostname,
        args.username,
        args.password,
        conn_mode=args.connection_mode,
    )
    vr.start()
