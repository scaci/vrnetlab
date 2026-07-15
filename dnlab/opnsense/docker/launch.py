#!/usr/bin/env python3

import datetime
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time

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


PATCH_SCRIPT_SRC = "/patch_config.py"
CONFIG_DRIVE_ISO = "/config-drive.iso"
CONFIG_STAGING = "/tmp/dnlab-cfg"
OPNSENSE_BOOTSTRAP_USERNAME = "root"
OPNSENSE_BOOTSTRAP_PASSWORD = "opnsense"


# If /persist is mounted, point base.qcow2 -> /installed.qcow2 so that
# vrnetlab.VM creates the overlay alongside the symlink (i.e. under /persist),
# making the overlay survive container restarts.
def _resolve_disk_image() -> str:
    base = "/installed.qcow2"
    persist_link = "/persist/base.qcow2"
    if os.path.isdir("/persist"):
        if not os.path.lexists(persist_link):
            os.symlink(base, persist_link)
        return persist_link
    return base


class DNLabOpnsenseVM(vrnetlab.VM):
    def __init__(self, hostname, username, password, nics, conn_mode):
        disk_image = _resolve_disk_image()

        super().__init__(
            username,
            password,
            disk_image=disk_image,
            ram=2048,
            driveif="virtio",
        )

        self.hostname = hostname
        self.conn_mode = conn_mode
        self.nic_type = "virtio-net-pci"
        self.num_nics = nics

        # Always ship the patcher via a config-drive ISO and run it on every
        # boot. OPNsense's first-boot scripts continue regenerating the
        # /conf/config.xml AFTER initial getty appears (and across full
        # reboots), wiping our in-place additions. Re-applying the idempotent
        # patcher on every boot is the simplest way to guarantee
        # nohttpreferercheck + opt1 stay present.
        self._generate_config_drive()
        self.qemu_args.extend(["-cdrom", CONFIG_DRIVE_ISO])

    @property
    def _marker_path(self) -> str:
        if os.path.isdir("/persist"):
            return "/persist/.dnlab-bootstrapped"
        return "/tmp/.dnlab-bootstrapped"

    def _bootstrap_needed(self) -> bool:
        return not os.path.exists(self._marker_path)

    def _generate_config_drive(self):
        os.makedirs(CONFIG_STAGING, exist_ok=True)
        import shutil
        shutil.copy(PATCH_SCRIPT_SRC, os.path.join(CONFIG_STAGING, "patch_config.py"))
        subprocess.run(
            [
                "genisoimage",
                "-quiet",
                "-R",  # Rock Ridge: preserve lowercase/long filenames for cd9660 mount
                "-V",
                "DNLAB-CFG",
                "-input-charset",
                "utf-8",
                "-o",
                CONFIG_DRIVE_ISO,
                CONFIG_STAGING,
            ],
            check=True,
        )
        self.logger.info("Generated %s (label DNLAB-CFG)", CONFIG_DRIVE_ISO)

    def _mark_bootstrapped(self):
        try:
            os.makedirs(os.path.dirname(self._marker_path), exist_ok=True)
        except FileExistsError:
            pass
        with open(self._marker_path, "w") as f:
            f.write(
                f"DNLab OPNsense bootstrap applied at {datetime.datetime.now().isoformat()}\n"
            )

    # ---- pexpect helpers ----

    def _drain(self, hint: str = ""):
        try:
            data = self.tn.read_very_eager()
            if data:
                self.logger.debug("DRAIN[%s] %r", hint, data[-300:])
        except Exception as e:
            self.logger.debug("DRAIN[%s] exc: %s", hint, e)

    def _read_until(self, pattern: bytes, timeout: int = 60, label: str = "") -> bytes:
        data = self.tn.read_until(pattern, timeout=timeout)
        if pattern not in data:
            tail = data[-400:].decode(errors="replace")
            raise TimeoutError(
                f"timed out waiting for {pattern!r} after {timeout}s (label={label!r}); "
                f"recent buffer: {tail!r}"
            )
        self.logger.debug("MATCH[%s] %r", label, data[-200:])
        return data

    def _send(self, s: str):
        self.tn.write(s.encode() + b"\r")

    def _patch_config_command(self, unique: str) -> str:
        mgmt_if = os.environ.get("DNLAB_OPNSENSE_MGMT_IF", "vtnet0")
        mgmt_alias = os.environ.get("DNLAB_OPNSENSE_MGMT_ALIAS", "opt9")
        dedicated_mgmt = (
            os.environ.get("DNLAB_OPNSENSE_DEDICATED_MGMT", "").lower() == "true"
        )

        args = [
            "python3 /tmp/dnlab-cd/patch_config.py",
            f"--bootstrap-hostname {shlex.quote(self.hostname)}",
            f"--mgmt-if {shlex.quote(mgmt_if)}",
            f"--mgmt-ipv4 {shlex.quote(self.mgmt_address_ipv4)}",
            f"--marker {shlex.quote(unique)}",
        ]
        if self.mgmt_gw_ipv4 and self.mgmt_gw_ipv4 != "dhcp":
            args.append(f"--mgmt-gw4 {shlex.quote(self.mgmt_gw_ipv4)}")
        if dedicated_mgmt:
            args.append("--dedicated-mgmt")
            args.append(f"--mgmt-alias {shlex.quote(mgmt_alias)}")
            args.append(f"--data-if-count {self.num_provisioned_nics}")

        return (
            f"/bin/sh -c '"
            f"mkdir -p /tmp/dnlab-cd && "
            f"mount -t cd9660 /dev/cd0 /tmp/dnlab-cd && "
            f"{' '.join(args)} && "
            f"umount /tmp/dnlab-cd"
            f"'"
        )

    def _do_first_boot_bootstrap(self):
        """Run the DNLab in-place patcher inside the running OPNsense.

        bootstrap_spin matched `login: `. From here:
          1. Settle 8s (let first-boot scripts finish flushing).
          2. login + Shell (option 8).
          3. Mount the config-drive CDROM (/dev/cd0).
          4. Run python3 /mnt/patch_config.py — idempotent in-place XML edit.
          5. configctl service reload all — apply without reboot.
          6. Verify nohttpreferercheck and the selected mgmt interface are present.
          7. Mark bootstrapped. NO REBOOT.
        """
        self.logger.info("First-boot bootstrap: settling 8s before login")
        time.sleep(8)
        self._drain("post-settle")

        # Containerlab's freebsd kind may pass lab credentials, but the OPNsense
        # serial console keeps the upstream defaults unless the guest config is
        # changed. Use the stable console credentials only for bootstrap.
        self._send(OPNSENSE_BOOTSTRAP_USERNAME)
        self._read_until(b"Password:", timeout=30, label="password-prompt")
        self._send(OPNSENSE_BOOTSTRAP_PASSWORD)
        self._read_until(b"Enter an option:", timeout=60, label="menu")
        self._send("8")
        self._read_until(b"OPNsense:~ # ", timeout=30, label="shell-prompt")
        self._drain("after-shell")

        # Patch the config in-place via /bin/sh wrapper (csh is unfriendly with ${}).
        unique = f"DNLAB_PATCH_{os.getpid()}"
        cmd = self._patch_config_command(unique)
        self._send(cmd)
        marker = f"{unique}_chk_".encode()
        data = self._read_until(marker, timeout=60, label="patch-result")
        tail = self._read_until(b"OPNsense:~ # ", timeout=20, label="post-patch-prompt")
        whole = data + tail
        m = re.search(
            rf"{unique}_chk_(\d+)_if_(\d+)_ip_(\d+)_sub_(\d+)".encode(),
            whole,
        )
        if not m:
            raise RuntimeError(
                f"Bootstrap: marker not found in output: {whole[-400:]!r}"
            )
        chk = int(m.group(1))
        intf = int(m.group(2))
        ip4 = int(m.group(3))
        subnet = int(m.group(4))
        self.logger.info(
            "Bootstrap: nohttpreferercheck=%d, mgmt interface=%d, ipaddr=%d, subnet=%d (expect all >=1)",
            chk, intf, ip4, subnet,
        )
        if chk < 1 or intf < 1 or ip4 < 1 or subnet < 1:
            raise RuntimeError(
                "Bootstrap: in-place patch did NOT take effect "
                f"(chk={chk}, intf={intf}, ipaddr={ip4}, subnet={subnet})"
            )

        # Apply via configctl reload (no reboot). Catch all services so
        # interfaces + filter + webgui are reconfigured.
        self.logger.info("Bootstrap: triggering configctl service reload all")
        self._send("/bin/sh -c 'configctl service reload all; echo DNLAB_RELOAD_DONE'")
        self._read_until(b"DNLAB_RELOAD_DONE", timeout=90, label="reload-done")
        self._read_until(b"OPNsense:~ # ", timeout=15, label="post-reload-prompt")

        # Mark bootstrapped on the container side BEFORE setting running.
        self._mark_bootstrapped()
        self.logger.info(
            "First-boot bootstrap complete; marker at %s", self._marker_path
        )

    def bootstrap_spin(self):
        if self.spins > 600:
            self.stop()
            self.start()
            return

        (ridx, match, res) = self.tn.expect([b"login: "], 1)
        if match and ridx == 0:
            self.logger.debug("matched, login: ")
            try:
                # Apply patch on every boot (idempotent). OPNsense's first-boot
                # logic regenerates /conf/config.xml across reboots — we
                # re-add our additions each time to keep them present.
                self._do_first_boot_bootstrap()
                self.running = True
                self.tn.close()
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info(
                    "Startup complete (DNLab patch applied) in: %s", startup_time
                )
                return
            except (TimeoutError, RuntimeError, OSError) as e:
                self.logger.error(
                    "Bootstrap failed: %s — will retry on next login", e
                )
                return  # retry on next iteration

        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            self.spins = 0

        self.spins += 1

    def gen_mgmt(self):
        # Force the mgmt NIC onto pci.1 so FreeBSD numbers it vtnet0
        # (same trick as freebsd/docker/launch.py).
        res = super().gen_mgmt()
        if "bus=pci.1" not in res[-3]:
            res[-3] = res[-3] + ",bus=pci.1"
        return res


class DNLabOpnsense(vrnetlab.VR):
    def __init__(self, hostname, username, password, nics, conn_mode):
        super().__init__(username, password)
        self.vms = [DNLabOpnsenseVM(hostname, username, password, nics, conn_mode)]


if __name__ == "__main__":
    import argparse

    # DNLab GUI sets RAM/VCPU in devices.json env, but vrnetlab.VM reads
    # QEMU_MEMORY/QEMU_SMP. Translate so the GUI values actually take effect.
    for src, dst in (("RAM", "QEMU_MEMORY"), ("VCPU", "QEMU_SMP")):
        if src in os.environ and dst not in os.environ:
            os.environ[dst] = os.environ[src]

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--username", default="root", help="Username")
    parser.add_argument("--password", default="opnsense", help="Password")
    parser.add_argument(
        "--hostname",
        default="opnsense",
        help="VM hostname (cosmetic; OPNsense reads its real hostname from config.xml)",
    )
    parser.add_argument("--nics", type=int, default=8, help="Number of NICs")
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

    vr = DNLabOpnsense(
        args.hostname,
        args.username,
        args.password,
        args.nics,
        args.connection_mode,
    )
    vr.start()
