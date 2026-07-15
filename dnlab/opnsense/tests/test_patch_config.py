from pathlib import Path
import importlib.util
import os


PATCH_PATH = Path(__file__).parents[1] / "docker" / "patch_config.py"
SPEC = importlib.util.spec_from_file_location("opnsense_patch_config", PATCH_PATH)
assert SPEC and SPEC.loader
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


def _backup(path: Path, name: str, hostname: str, mtime: int) -> None:
    target = path / name
    target.write_text(
        f"<opnsense><system><hostname>{hostname}</hostname></system></opnsense>"
    )
    os.utime(target, (mtime, mtime))


def test_current_guest_hostname_is_authoritative(tmp_path):
    assert PATCH.resolve_hostname("edge-fw", "opnsen1", "old-fw", tmp_path) == "edge-fw"


def test_latest_non_default_backup_survives_interface_reset(tmp_path):
    _backup(tmp_path, "config-1.xml", "old-fw", 1)
    _backup(tmp_path, "config-2.xml", "new-fw", 2)

    assert PATCH.resolve_hostname("OPNsense", "opnsen1", "sidecar-fw", tmp_path) == "new-fw"


def test_saved_hostname_is_fallback_when_backups_are_missing(tmp_path):
    assert PATCH.resolve_hostname("OPNsense", "opnsen1", "saved-fw", tmp_path) == "saved-fw"


def test_topology_name_is_bootstrap_only(tmp_path):
    assert PATCH.resolve_hostname("OPNsense", "opnsen1", "", tmp_path) == "opnsen1"
