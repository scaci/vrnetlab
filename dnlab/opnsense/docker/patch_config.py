#!/usr/local/bin/python3
"""DNLab in-place patcher for OPNsense /conf/config.xml.

Runs INSIDE the OPNsense guest, after first-boot has settled (and OPNsense
has regenerated its WebGUI cert with a fresh refid). Adds DNLab-specific
elements without touching the factory cert / lan / etc., so OPNsense's
config validator doesn't trigger a factory revert on the next service reload.

Idempotent: re-running the patch is a no-op if the elements already exist.
"""

import argparse
import ipaddress
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

CONFIG_PATH = "/conf/config.xml"
CONFIG_BACKUP_DIR = "/conf/backup"
HOSTNAME_STATE_PATH = "/conf/.dnlab-hostname"
DEFAULT_MGMT_IF = "vtnet0"
DEFAULT_MGMT_ALIAS = "opt9"
DEFAULT_LAN_IP = "192.168.1.1"
DEFAULT_LAN_PREFIX = "24"
DEFAULT_HOSTNAMES = {"opnsense"}
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def valid_hostname(value: str | None) -> str:
    value = (value or "").strip()
    return value if HOSTNAME_RE.fullmatch(value) else ""


def hostname_from_config(path: Path) -> str:
    try:
        return valid_hostname(ET.parse(path).getroot().findtext("./system/hostname"))
    except (OSError, ET.ParseError):
        return ""


def resolve_hostname(
    current: str,
    bootstrap: str,
    saved: str,
    backup_dir: Path,
) -> str:
    current = valid_hostname(current)
    bootstrap = valid_hostname(bootstrap)
    saved = valid_hostname(saved)

    if current and current.lower() not in DEFAULT_HOSTNAMES:
        return current

    try:
        backups = sorted(
            backup_dir.glob("config-*.xml"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        backups = []
    for backup in backups:
        candidate = hostname_from_config(backup)
        if candidate and candidate.lower() not in DEFAULT_HOSTNAMES:
            return candidate

    return saved or bootstrap or current or "OPNsense"


def preserve_hostname(root: ET.Element, bootstrap: str) -> list[str]:
    system = root.find("./system")
    if system is None:
        return []
    try:
        saved = Path(HOSTNAME_STATE_PATH).read_text().strip()
    except OSError:
        saved = ""
    desired = resolve_hostname(
        system.findtext("hostname") or "",
        bootstrap,
        saved,
        Path(CONFIG_BACKUP_DIR),
    )
    changes = []
    if system.findtext("hostname") != desired:
        ensure_child(system, "hostname", desired)
        changes.append(f"hostname={desired}")
    try:
        Path(HOSTNAME_STATE_PATH).write_text(f"{desired}\n")
    except OSError as exc:
        print(f"WARNING: cannot persist hostname state: {exc}", file=sys.stderr)
    return changes


def ensure_child(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    if text is not None and child.text != text:
        child.text = text
    return child


def remove_child(parent: ET.Element, tag: str) -> None:
    child = parent.find(tag)
    if child is not None:
        parent.remove(child)


def set_static_interface(
    intf: ET.Element,
    name: str,
    mgmt_ipv4: ipaddress.IPv4Interface,
    mgmt_gw4: str | None,
) -> list[str]:
    changes = []
    desired = {
        "enable": "1",
        "if": name,
        "ipaddr": str(mgmt_ipv4.ip),
        "subnet": str(mgmt_ipv4.network.prefixlen),
    }
    if mgmt_gw4:
        desired["gateway"] = mgmt_gw4

    for tag, value in desired.items():
        before = intf.findtext(tag)
        ensure_child(intf, tag, value)
        if before != value:
            changes.append(f"{tag}={value}")

    remove_child(intf, "ipaddrv6")
    remove_child(intf, "subnetv6")
    if not mgmt_gw4:
        remove_child(intf, "gateway")
    for tag in ("media", "mediaopt", "gatewayv6"):
        ensure_child(intf, tag)
    return changes


def set_text_interface(
    intf: ET.Element,
    name: str,
    descr: str,
    values: dict[str, str | None],
) -> list[str]:
    changes = []
    desired = {
        "enable": "1",
        "if": name,
        "descr": descr,
        **values,
    }
    for tag, value in desired.items():
        if value is None:
            remove_child(intf, tag)
            continue
        before = intf.findtext(tag)
        ensure_child(intf, tag, value)
        if before != value:
            changes.append(f"{tag}={value}")

    for tag in ("media", "mediaopt", "gateway", "gatewayv6"):
        ensure_child(intf, tag)
    return changes


def ensure_pass_rule(root: ET.Element, interface: str, descr: str) -> list[str]:
    filt = root.find("./filter")
    if filt is None:
        filt = ET.SubElement(root, "filter")
    have_dnlab_rule = any(
        r.findtext("descr") == descr and r.findtext("interface") == interface
        for r in filt.findall("rule")
    )
    if have_dnlab_rule:
        return []

    rule = ET.SubElement(filt, "rule")
    ET.SubElement(rule, "type").text = "pass"
    ET.SubElement(rule, "ipprotocol").text = "inet"
    ET.SubElement(rule, "descr").text = descr
    ET.SubElement(rule, "interface").text = interface
    src = ET.SubElement(rule, "source")
    ET.SubElement(src, "any")
    dst = ET.SubElement(rule, "destination")
    ET.SubElement(dst, "any")
    return ["filter-rule"]


def configure_shifted_dataplane(
    interfaces: ET.Element,
    data_if_count: int,
    mgmt_alias: str,
) -> list[str]:
    changes = []
    if data_if_count >= 1:
        lan = interfaces.find("lan")
        if lan is None:
            lan = ET.SubElement(interfaces, "lan")
            changes.append("lan")
        lan_changes = set_text_interface(
            lan,
            "vtnet1",
            "LAN",
            {
                "ipaddr": DEFAULT_LAN_IP,
                "subnet": DEFAULT_LAN_PREFIX,
                "ipaddrv6": None,
                "subnetv6": None,
            },
        )
        changes.extend(f"lan-{change}" for change in lan_changes)

    if data_if_count >= 2:
        wan = interfaces.find("wan")
        if wan is None:
            wan = ET.SubElement(interfaces, "wan")
            changes.append("wan")
        wan_changes = set_text_interface(
            wan,
            "vtnet2",
            "WAN",
            {
                "ipaddr": "dhcp",
                "ipaddrv6": "dhcp6",
                "subnet": None,
                "subnetv6": None,
            },
        )
        changes.extend(f"wan-{change}" for change in wan_changes)

    for idx in range(3, data_if_count + 1):
        opt_num = idx - 2
        alias = f"opt{opt_num}"
        # The DNLab management alias is reserved and should not be reused for
        # dataplane assignment.
        if alias == mgmt_alias:
            alias = f"opt{opt_num + 1}"
        opt = interfaces.find(alias)
        if opt is None:
            opt = ET.SubElement(interfaces, alias)
            changes.append(alias)
        opt_changes = set_text_interface(
            opt,
            f"vtnet{idx}",
            alias.upper(),
            {
                "ipaddr": None,
                "ipaddrv6": None,
                "subnet": None,
                "subnetv6": None,
            },
        )
        changes.extend(f"{alias}-{change}" for change in opt_changes)
    return changes


def emit_marker(
    marker: str | None,
    root: ET.Element,
    mgmt_alias: str,
    mgmt_ipv4: ipaddress.IPv4Interface,
) -> None:
    if not marker:
        return

    webgui = root.find("./system/webgui")
    intf = root.find(f"./interfaces/{mgmt_alias}")
    chk = 1 if webgui is not None and webgui.find("nohttpreferercheck") is not None else 0
    ifc = 1 if intf is not None else 0
    ip4 = 1 if intf is not None and intf.findtext("ipaddr") == str(mgmt_ipv4.ip) else 0
    subnet = (
        1
        if intf is not None
        and intf.findtext("subnet") == str(mgmt_ipv4.network.prefixlen)
        else 0
    )
    print(f"{marker}_chk_{chk}_if_{ifc}_ip_{ip4}_sub_{subnet}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mgmt-if", default=DEFAULT_MGMT_IF)
    parser.add_argument("--bootstrap-hostname", default="")
    parser.add_argument("--mgmt-ipv4", required=True)
    parser.add_argument("--mgmt-gw4")
    parser.add_argument("--dedicated-mgmt", action="store_true")
    parser.add_argument("--mgmt-alias", default=DEFAULT_MGMT_ALIAS)
    parser.add_argument("--data-if-count", type=int, default=0)
    parser.add_argument("--marker")
    args = parser.parse_args()

    try:
        mgmt_ipv4 = ipaddress.ip_interface(args.mgmt_ipv4)
    except ValueError as exc:
        print(f"ERROR: invalid --mgmt-ipv4 {args.mgmt_ipv4!r}: {exc}", file=sys.stderr)
        return 1
    if mgmt_ipv4.version != 4:
        print("ERROR: --mgmt-ipv4 must be an IPv4 interface", file=sys.stderr)
        return 1

    tree = ET.parse(CONFIG_PATH)
    root = tree.getroot()
    changes = preserve_hostname(root, args.bootstrap_hostname)

    # 1. <system><webgui><nohttpreferercheck>1</nohttpreferercheck>
    webgui = root.find("./system/webgui")
    if webgui is None:
        print("ERROR: <system><webgui> not found in config", file=sys.stderr)
        return 1
    if webgui.find("nohttpreferercheck") is None:
        nhrc = ET.SubElement(webgui, "nohttpreferercheck")
        nhrc.text = "1"
        changes.append("nohttpreferercheck")

    # 2. Default DNLab mgmt is LAN on vtnet0. Dedicated mode uses a separate
    # alias on vtnet0 and shifts data ports to vtnet1, vtnet2, ...
    interfaces = root.find("./interfaces")
    if interfaces is None:
        print("ERROR: <interfaces> not found", file=sys.stderr)
        return 1

    mgmt_alias = args.mgmt_alias if args.dedicated_mgmt else "lan"
    mgmt_descr = "MGMT" if args.dedicated_mgmt else "LAN"
    mgmt_elem = interfaces.find(mgmt_alias)
    if mgmt_elem is None:
        mgmt_elem = ET.SubElement(interfaces, mgmt_alias)
        changes.append(mgmt_alias)

    if mgmt_elem.findtext("descr") != mgmt_descr:
        ensure_child(mgmt_elem, "descr", mgmt_descr)
        changes.append(f"{mgmt_alias}-descr")
    interface_changes = set_static_interface(
        mgmt_elem,
        args.mgmt_if,
        mgmt_ipv4,
        args.mgmt_gw4,
    )
    changes.extend(f"{mgmt_alias}-{change}" for change in interface_changes)

    if args.dedicated_mgmt:
        changes.extend(
            configure_shifted_dataplane(
                interfaces,
                args.data_if_count,
                mgmt_alias,
            )
        )
        changes.extend(
            ensure_pass_rule(
                root,
                mgmt_alias,
                f"DNLab: allow MGMT ({mgmt_alias}) inbound",
            )
        )

    # 3. WebGUI bind: LAN by default, dedicated MGMT alias in opt-in mode.
    intf_elem = webgui.find("interfaces")
    desired_webgui_interfaces = mgmt_alias if args.dedicated_mgmt else "lan"
    if intf_elem is None:
        intf_elem = ET.SubElement(webgui, "interfaces")
        intf_elem.text = desired_webgui_interfaces
        changes.append("webgui-bind")
    elif (intf_elem.text or "") != desired_webgui_interfaces:
        intf_elem.text = desired_webgui_interfaces
        changes.append("webgui-bind-update")

    if not changes:
        print("DNLAB_PATCH_NOOP")
        emit_marker(args.marker, root, mgmt_alias, mgmt_ipv4)
        return 0

    tree.write(CONFIG_PATH, encoding="UTF-8", xml_declaration=True)
    print("DNLAB_PATCH_APPLIED:", ",".join(changes))
    emit_marker(args.marker, root, mgmt_alias, mgmt_ipv4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
