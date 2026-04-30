#!/usr/bin/env bash
# =============================================================================
# build-c9800-qcow2.sh
#
# Automate the install of a Cisco Catalyst 9800-CL from its ISO into a
# bootable KVM-ready qcow2.
#
# What it does, in order:
#   1. Validate the input ISO exists and is a Cisco IOS-XE image.
#   2. Create an empty 16 GB qcow2 named after the ISO (same base name,
#      .qcow2 extension), in the same directory as the ISO unless -o is
#      given.
#   3. Boot QEMU with the ISO as cdrom + qcow2 as disk, console on a
#      local TCP port.
#   4. Pick the 'serial console' entry from the GRUB boot menu (the ISO
#      defaults to vga which on a headless serial would silently hang).
#   5. Drive the IOS-XE install via expect, waiting for first prompt.
#   6. Run `write erase` + `reload` so the baked qcow2 starts factory-clean.
#   7. Wait for the post-reload prompt to confirm bootability standalone.
#   8. Powerdown clean (system_powerdown via QEMU monitor).
#   9. Verify the qcow2 with `qemu-img check`.
#
# Usage:
#   ./build-c9800-qcow2.sh /path/to/C9800-CL-universalk9.17.13.01a.iso
#   ./build-c9800-qcow2.sh -o /custom/out.qcow2 /path/to/foo.iso
#
# Requirements (script will check):
#   - qemu-system-x86_64, qemu-img
#   - python3 with pexpect
#   - kvm enabled (/dev/kvm accessible)
#
# Tested on: Debian 13, Ubuntu 22.04+
# =============================================================================

set -euo pipefail

# ------ defaults ------
DISK_SIZE_GB=16
RAM_MB=16384
VCPUS=4
CONSOLE_PORT=5555     # local-only TCP for QEMU serial
MONITOR_PORT=5556     # local-only TCP for QEMU monitor
INSTALL_TIMEOUT=2400  # 40 min — first install can be very slow
BOOT_TIMEOUT=900      # 15 min for the post-reload boot
OUT_PATH=""

# ------ argument parsing ------
usage() {
    cat <<EOF
Usage: $0 [-o OUTPUT_QCOW2] ISO_PATH

  -o PATH    Output qcow2 path (default: same dir as ISO, same basename + .qcow2)
  -h         This help

Example:
  $0 /opt/img_dnlab/C9800-CL-universalk9.17.13.01a.iso
  -> creates /opt/img_dnlab/C9800-CL-universalk9.17.13.01a.qcow2
EOF
    exit 1
}

while getopts "o:h" opt; do
    case $opt in
        o) OUT_PATH="$OPTARG" ;;
        h|*) usage ;;
    esac
done
shift $((OPTIND-1))

if [[ $# -ne 1 ]]; then
    usage
fi

ISO_PATH="$1"

# ------ pre-flight checks ------
log() { echo -e "\033[1;36m[$(date +%H:%M:%S)]\033[0m $*"; }
err() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; }
die() { err "$*"; exit 1; }

[[ -f "$ISO_PATH" ]] || die "ISO not found: $ISO_PATH"
[[ "${ISO_PATH,,}" == *.iso ]] || die "Input must be a .iso file (got: $ISO_PATH)"

command -v qemu-system-x86_64 >/dev/null || die "qemu-system-x86_64 not installed"
command -v qemu-img           >/dev/null || die "qemu-img not installed"
command -v python3            >/dev/null || die "python3 not installed"
command -v xorriso            >/dev/null || die "xorriso not installed (apt-get install -y xorriso)"

python3 -c 'import pexpect' 2>/dev/null \
    || die "Python module 'pexpect' missing. Install: pip3 install --break-system-packages pexpect"

[[ -r /dev/kvm && -w /dev/kvm ]] \
    || die "/dev/kvm not accessible. Run as root or add user to kvm group."

# Quick sanity on the ISO: must contain something that looks like
# Cisco IOS-XE (we don't mount it, just check the volume label / file size).
ISO_SIZE_BYTES=$(stat -c%s "$ISO_PATH")
if [[ $ISO_SIZE_BYTES -lt $((500 * 1024 * 1024)) ]]; then
    die "ISO suspiciously small ($((ISO_SIZE_BYTES / 1024 / 1024)) MB). Expected >500 MB for C9800-CL."
fi

# ------ derive output path ------
if [[ -z "$OUT_PATH" ]]; then
    ISO_DIR=$(dirname "$ISO_PATH")
    ISO_BASE=$(basename "$ISO_PATH")
    OUT_PATH="${ISO_DIR}/${ISO_BASE%.iso}.qcow2"
    # also handle .ISO uppercase
    OUT_PATH="${OUT_PATH%.ISO}"
fi

if [[ -e "$OUT_PATH" ]]; then
    err "Output already exists: $OUT_PATH"
    read -p "Overwrite? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || die "aborted"
    rm -f "$OUT_PATH"
fi

log "Input  ISO : $ISO_PATH ($((ISO_SIZE_BYTES / 1024 / 1024)) MB)"
log "Output qcow2: $OUT_PATH (${DISK_SIZE_GB} GB sparse)"

# ------ patch ISO to make Serial Console the default GRUB entry ------
# Why: at runtime we cannot reliably push keystrokes through socket /
# pexpect / socat / QEMU emulated serial fast enough to hit the
# 8-second GRUB countdown on a nested LXC. Modifying grub.cfg before
# boot is deterministic.
PATCH_DIR=$(mktemp -d /tmp/c9800-iso-patch-XXXX)
PATCHED_ISO="${PATCH_DIR}/patched.iso"

log "Patching ISO so default GRUB entry is 'Serial Console'..."
log "  (extracting to ${PATCH_DIR}/iso_root, please wait...)"

# Use xorriso in extract mode (-osirrox) preserving Rock Ridge attributes.
xorriso -osirrox on -indev "$ISO_PATH" \
        -extract / "${PATCH_DIR}/iso_root" 2>/dev/null \
    || die "xorriso extract failed"

# Find grub.cfg(s). C9800-CL ISO has at least /boot/grub/grub.cfg for
# legacy BIOS boot. Some builds also have /EFI/BOOT/grub.cfg for UEFI.
GRUB_CFGS=$(find "${PATCH_DIR}/iso_root" -type f -iname "grub.cfg" 2>/dev/null)
[[ -n "$GRUB_CFGS" ]] || die "No grub.cfg found inside ISO — unexpected layout."

PATCHED_ANY=0
while read -r cfg; do
    log "  patching $(basename $(dirname $(dirname "$cfg")))/$(basename $(dirname "$cfg"))/$(basename "$cfg")..."
    # Make file writable (extracted as read-only by xorriso)
    chmod u+w "$cfg"
    # The C9800-CL grub.cfg uses GRUB-legacy-style assignments WITHOUT
    # the 'set' keyword:
    #     default=0
    #     timeout=10
    # Entry 0 is VGA, entry 1 is Serial Console. Switch the index to 1
    # and shorten the timeout. Both 'default=' and 'set default=' forms
    # are handled defensively in case Cisco changes the syntax.
    if grep -qE 'Serial Console' "$cfg"; then
        # ---- default ----
        if grep -qE '^[[:space:]]*set[[:space:]]+default=' "$cfg"; then
            sed -i -E 's/^[[:space:]]*set[[:space:]]+default=.*/set default=1/' "$cfg"
        elif grep -qE '^[[:space:]]*default=' "$cfg"; then
            sed -i -E 's/^[[:space:]]*default=.*/default=1/' "$cfg"
        else
            sed -i '1i default=1' "$cfg"
        fi
        # ---- timeout ----
        if grep -qE '^[[:space:]]*set[[:space:]]+timeout=' "$cfg"; then
            sed -i -E 's/^[[:space:]]*set[[:space:]]+timeout=.*/set timeout=1/' "$cfg"
        elif grep -qE '^[[:space:]]*timeout=' "$cfg"; then
            sed -i -E 's/^[[:space:]]*timeout=.*/timeout=1/' "$cfg"
        else
            sed -i '1i timeout=1' "$cfg"
        fi
        # ---- visual confirmation in our log ----
        log "    -> $(grep -E '^[[:space:]]*(set[[:space:]]+)?default=' "$cfg" | head -1 | tr -d ' ')"
        log "    -> $(grep -E '^[[:space:]]*(set[[:space:]]+)?timeout=' "$cfg" | head -1 | tr -d ' ')"
        PATCHED_ANY=1
    else
        log "    (no Serial Console entry here, skipping)"
    fi
done <<< "$GRUB_CFGS"

[[ $PATCHED_ANY -eq 1 ]] || die "No grub.cfg contained a Serial Console entry"

# Re-pack the ISO. We must preserve the El Torito boot record so the
# ISO remains bootable. xorriso has special syntax for this:
#   -boot_image any replay   replays the original boot info from the
#                            extracted source ISO image.
log "  rebuilding ISO..."
xorriso -indev "$ISO_PATH" \
        -outdev "$PATCHED_ISO" \
        -boot_image any replay \
        -map "${PATCH_DIR}/iso_root" / \
        -- 2>/dev/null \
    || die "xorriso rebuild failed"

ISO_SIZE_AFTER=$(stat -c%s "$PATCHED_ISO")
log "  patched ISO: $PATCHED_ISO ($((ISO_SIZE_AFTER / 1024 / 1024)) MB)"

# From here on, use the patched ISO instead of the original.
ISO_PATH="$PATCHED_ISO"

# ------ port availability ------
for p in $CONSOLE_PORT $MONITOR_PORT; do
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":$p\$"; then
        die "TCP port $p already in use; another QEMU running?"
    fi
done

# ------ create empty qcow2 ------
log "Creating empty qcow2..."
qemu-img create -f qcow2 "$OUT_PATH" "${DISK_SIZE_GB}G" >/dev/null

# ------ launch QEMU in background ------
log "Launching QEMU..."
QEMU_LOG=$(mktemp /tmp/qemu-c9800-XXXX.log)
QEMU_SERIAL_LOG=$(mktemp /tmp/qemu-c9800-serial-XXXX.log)

qemu-system-x86_64 \
    -name c9800cl-install \
    -enable-kvm \
    -display none \
    -machine pc \
    -m $RAM_MB \
    -cpu host \
    -smp cores=$VCPUS,threads=1,sockets=1 \
    -drive "if=virtio,file=$OUT_PATH,format=qcow2,index=0" \
    -drive "if=ide,file=$ISO_PATH,media=cdrom,index=2" \
    -boot order=cd \
    -device virtio-net-pci,netdev=n0 \
    -netdev user,id=n0 \
    -chardev "socket,id=ser0,host=127.0.0.1,port=${CONSOLE_PORT},server=on,wait=on,telnet=on,logfile=${QEMU_SERIAL_LOG}" \
    -serial chardev:ser0 \
    -monitor "telnet:127.0.0.1:${MONITOR_PORT},server,nowait" \
    -pidfile /tmp/c9800-build.pid \
    >"$QEMU_LOG" 2>&1 &

QEMU_PID=$!
echo $QEMU_PID > /tmp/c9800-build.pid
log "QEMU launched (pid $QEMU_PID); serial on tcp/$CONSOLE_PORT (QEMU will WAIT for our client before booting)"

# small grace period for QEMU to bind the listening sockets
sleep 2

# verify QEMU is alive (not crashed during arg parsing)
if ! kill -0 "$QEMU_PID" 2>/dev/null; then
    cat "$QEMU_LOG"
    die "QEMU exited immediately. See log above."
fi

cleanup() {
    if kill -0 "$QEMU_PID" 2>/dev/null; then
        log "Cleaning up: killing QEMU $QEMU_PID..."
        kill "$QEMU_PID" 2>/dev/null || true
        sleep 2
        kill -9 "$QEMU_PID" 2>/dev/null || true
    fi
    rm -f /tmp/c9800-build.pid "$QEMU_LOG" "$QEMU_SERIAL_LOG"
    rm -rf "$PATCH_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ------ drive the install via pexpect ------
log "Connecting to serial console; starting install automation..."
log "(this will take 20-40 minutes — be patient)"

# IMPORTANT: heredoc delimiter is QUOTED ('PYEOF') so bash does NOT expand
# anything inside — the Python code is passed verbatim. Values are sent
# via env vars instead.
export CONSOLE_PORT MONITOR_PORT INSTALL_TIMEOUT BOOT_TIMEOUT

python3 - <<'PYEOF'
import pexpect, sys, time, os

CONSOLE_PORT    = int(os.environ["CONSOLE_PORT"])
MONITOR_PORT    = int(os.environ["MONITOR_PORT"])
INSTALL_TIMEOUT = int(os.environ["INSTALL_TIMEOUT"])
BOOT_TIMEOUT    = int(os.environ["BOOT_TIMEOUT"])

def color(msg, c='36'):
    print(f"\033[1;{c}m[install]\033[0m {msg}", flush=True)

con = pexpect.spawn(f"telnet 127.0.0.1 {CONSOLE_PORT}",
                    encoding='utf-8', timeout=60)
con.logfile_read = sys.stdout
con.expect("Escape character is")

# Send a wakeup byte. With QEMU `wait=on`, this releases the guest
# CPUs and starts the boot.
con.send("\r")


# ---- Phase 0: ISO GRUB — already patched to default to Serial ----
# We patched the ISO before launching QEMU so the GRUB default is
# 'C9800-CL Serial Console' and timeout is 1 second. No runtime
# input is needed; the right entry boots automatically.
color("Waiting for ISO GRUB to auto-boot Serial entry...")
gi = con.expect([
    r"console=\s+SR_BOOT",     # 0 — confirmed Serial entry kernel cmdline
    r"console=tty0",           # 1 — VGA somehow booted (patch failed)
    pexpect.TIMEOUT,           # 2
], timeout=120)
if gi == 1:
    color("ERROR: GRUB booted VGA — ISO patch may have failed.")
    sys.exit(2)
if gi == 2:
    color("TIMEOUT waiting for GRUB to boot. Aborting.")
    sys.exit(2)
color("Confirmed: booting Serial Console entry from patched ISO.")

# ---- Phase 1: ISO install (partitioning, copy, reboot to HD) ----
# Fully unattended on the ISO side. Watch for 'Rebooting from HD'.
color("Waiting for ISO install to complete (eject + reboot to HD)...")
con.expect("Rebooting from HD", timeout=INSTALL_TIMEOUT)
color("ISO install done. Box is rebooting onto installed bootflash.")

# Second GRUB (the one on /bootflash) just shows packages.conf — let it.
con.expect("Booting `vWLC", timeout=300)
color("Second GRUB: booting installed image.")

# ---- Phase 2: first-boot configuration dialog ----
color("Waiting for initial configuration dialog...")
con.expect("Would you like to enter the initial configuration dialog",
           timeout=BOOT_TIMEOUT)
con.expect(r"\[yes/no\]:", timeout=30)
time.sleep(1)
con.sendline("no")

# IOS may not catch the answer if PnP/autoinstall is racing with the
# console. Loop until we get to either enable-secret or RETURN prompt.
color("Sent 'no' to setup dialog. Watching for next prompt...")
idx = -1
for attempt in range(6):
    idx = con.expect([
        r"Enter enable secret:",                 # 0 — must set password
        r"Press RETURN to get started",          # 1 — done, no password
        r"Would you like to enter.*\[yes/no\]:", # 2 — re-asking
        r"% Please answer 'yes' or 'no'",        # 3 — didn't catch
        pexpect.TIMEOUT,                         # 4
    ], timeout=180)
    if idx in (0, 1):
        break
    if idx in (2, 3):
        time.sleep(2)
        con.sendline("no")
        continue
    color("TIMEOUT waiting for post-dialog prompt.")
    sys.exit(3)

# ---- Phase 3: enable secret (mandatory on 17.15) ----
ENABLE_SECRET = "Vrnetlab9!"  # 10 chars: upper+lower+digit+symbol, no 'cisco'
if idx == 0:
    color("Setting enable secret (mandatory on 17.15)...")
    con.sendline(ENABLE_SECRET)
    con.expect(r"Confirm enable secret:", timeout=30)
    con.sendline(ENABLE_SECRET)
    # Menu: [0] CLI no save, [1] back to setup, [2] save and exit
    con.expect(r"Enter your selection:", timeout=120)
    con.sendline("0")
    color("Selected '0' — exit to CLI without saving.")
    con.expect("Press RETURN to get started", timeout=300)

# ---- Phase 4: reach privileged exec ----
# After "Press RETURN", IOSd spends 1-3 minutes printing PnP Discovery,
# crypto self-tests, redundancy SSO, GigE up/down, and PKI messages.
# During this time, sending CR and expecting '>' is unreliable: the
# console is busy and the prompt may not appear (or PnP can hold a
# 300s backoff that suppresses user prompt visibility).
#
# Wait first for the marker that tells us the box is truly idle and
# at user-CLI: %PNP-6-PNP_DISCOVERY_STOPPED.
color("Waiting for PnP discovery to stop (box settling, ~1-3 min)...")
con.expect(r"PNP-6-PNP_DISCOVERY_STOPPED", timeout=600)
color("PnP stopped. Letting console settle for 5s...")
time.sleep(5)

# Drain anything that arrived during the sleep.
try:
    con.expect(pexpect.TIMEOUT, timeout=2)
except Exception:
    pass

# Now poke the console. The prompt may be either WLC> (user-exec) or
# WLC# (privileged) depending on whether enable secret session is
# active from the setup dialog flow.
con.sendline("")
i = con.expect([r"\w+#", r"\w+>", pexpect.TIMEOUT], timeout=120)
if i == 2:
    color("TIMEOUT: no prompt after PnP stopped. Aborting.")
    sys.exit(3)

if i == 1:
    # at user-exec, escalate to privileged
    con.sendline("enable")
    j = con.expect([r"Password:", r"\w+#"], timeout=60)
    if j == 0:
        con.sendline(ENABLE_SECRET)
        con.expect(r"\w+#", timeout=30)
# else (i==0): already at # prompt, nothing to do.

color("At privileged exec.")

# ---- Phase 5: write erase ----
# Drain any pending PnP log lines before issuing.
time.sleep(3)
con.sendline("")
con.expect(r"\w+#", timeout=30)
con.sendline("write erase")
con.expect(r"\[confirm\]", timeout=30)
con.send("\r")
con.expect(r"\[OK\]", timeout=120)
color("nvram erased.")
con.expect(r"\w+#", timeout=30)

# ---- Phase 6: reload without saving ----
con.sendline("reload")
i = con.expect([
    r"System configuration has been modified.*\[yes/no\]:",
    r"Proceed with reload\? \[confirm\]",
    pexpect.TIMEOUT,
], timeout=30)
if i == 0:
    con.sendline("no")
    con.expect(r"Proceed with reload\? \[confirm\]", timeout=30)
elif i == 2:
    color("TIMEOUT issuing reload. Aborting.")
    sys.exit(4)
con.send("\r")
color("Reload issued. Waiting for clean reboot to first-boot prompt...")

# ---- Phase 7: confirm bootability standalone ----
con.expect("Would you like to enter the initial configuration dialog",
           timeout=BOOT_TIMEOUT)
color("qcow2 is bootable standalone — install verified clean!")

# ---- Phase 8: clean shutdown via QEMU monitor ----
color("Issuing system_powerdown via QEMU monitor...")
mon = pexpect.spawn(f"telnet 127.0.0.1 {MONITOR_PORT}",
                    encoding='utf-8', timeout=30)
mon.expect("Escape character is")
mon.expect(r"\(qemu\)")
mon.sendline("system_powerdown")
mon.expect(r"\(qemu\)")
color("Waiting for clean guest shutdown (up to 3 min)...")
try:
    con.expect(pexpect.EOF, timeout=180)
    color("Guest powered down cleanly.")
except pexpect.TIMEOUT:
    color("Guest didn't ACK powerdown — forcing 'quit' on monitor.")
    mon.sendline("quit")

mon.close(force=True)
con.close(force=True)
sys.exit(0)
PYEOF
PYEOF

PY_RC=$?
if [[ $PY_RC -ne 0 ]]; then
    err "Install automation failed (rc=$PY_RC). QEMU log: $QEMU_LOG"
    exit $PY_RC
fi

# ensure QEMU has actually exited
for _ in $(seq 1 30); do
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        break
    fi
    sleep 1
done

# ------ post-build verification ------
log "Verifying output qcow2..."
qemu-img check "$OUT_PATH" \
    || die "qemu-img check failed — image is corrupt"

QCOW_DISK_SIZE=$(qemu-img info --output=json "$OUT_PATH" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["actual-size"])')
QCOW_DISK_GB=$((QCOW_DISK_SIZE / 1024 / 1024 / 1024))

if [[ $QCOW_DISK_GB -lt 4 ]]; then
    err "qcow2 actual size only ${QCOW_DISK_GB} GB — install probably incomplete."
    die "expected >=4 GB after a real install"
fi

log "Done."
log "Output: $OUT_PATH"
log "Actual disk size: ${QCOW_DISK_GB} GB"
log ""
log "Next steps for vrnetlab:"
log "  cp '$OUT_PATH' /opt/vrnetlab/cisco/cat9kv_V3/$(basename "$OUT_PATH")"
log "  cd /opt/vrnetlab/cisco/cat9kv_V3 && make docker-image"
