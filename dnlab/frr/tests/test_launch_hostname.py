from pathlib import Path
import importlib.util
import os
import sys
import types


class _VM:
    pass


class _VR:
    pass


sys.modules.setdefault("vrnetlab", types.SimpleNamespace(VM=_VM, VR=_VR))
LAUNCH_PATH = Path(__file__).parents[1] / "docker" / "launch.py"
SPEC = importlib.util.spec_from_file_location("dnlab_frr_launch", LAUNCH_PATH)
assert SPEC and SPEC.loader
LAUNCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCH)


def test_new_config_uses_topology_hostname(tmp_path):
    config = tmp_path / "frr.conf"

    assert LAUNCH._resolve_persistent_hostname("frr1", str(config)) == "frr1"
    assert "hostname " not in config.read_text()


def test_default_hostname_is_repaired_without_changing_other_config(tmp_path):
    config = tmp_path / "frr.conf"
    config.write_text(
        "frr version 10.6.1\n"
        "frr defaults traditional\n"
        "hostname dnlab-frr\n"
        "router bgp 65000\n"
        " neighbor 10.0.0.2 remote-as 65000\n"
    )

    assert LAUNCH._resolve_persistent_hostname("frr2", str(config)) == "frr2"
    assert config.read_text() == (
        "frr version 10.6.1\n"
        "frr defaults traditional\n"
        "router bgp 65000\n"
        " neighbor 10.0.0.2 remote-as 65000\n"
    )


def test_custom_hostname_is_authoritative_and_file_is_untouched(tmp_path):
    config = tmp_path / "frr.conf"
    original = "frr defaults traditional\nhostname edge-router\nrouter ospf\n"
    config.write_text(original)

    assert LAUNCH._resolve_persistent_hostname("frr2", str(config)) == "edge-router"
    assert config.read_text() == original


def test_last_custom_hostname_wins_and_duplicates_are_normalized(tmp_path):
    config = tmp_path / "frr.conf"
    config.write_text(
        "frr defaults traditional\n"
        "hostname frr\n"
        "hostname persisted-frr\n"
        "router bgp 65000\n"
    )

    assert LAUNCH._resolve_persistent_hostname("frr2", str(config)) == "persisted-frr"
    assert config.read_text() == (
        "frr defaults traditional\n"
        "hostname persisted-frr\n"
        "router bgp 65000\n"
    )


def test_invalid_configured_hostname_falls_back_safely(tmp_path):
    config = tmp_path / "frr.conf"
    config.write_text("frr defaults traditional\nhostname invalid_name\n")

    assert LAUNCH._resolve_persistent_hostname("frr3", str(config)) == "frr3"
    assert "hostname " not in config.read_text()


def test_atomic_rewrite_preserves_mode(tmp_path):
    config = tmp_path / "frr.conf"
    config.write_text("hostname dnlab-frr\n")
    config.chmod(0o600)

    LAUNCH._resolve_persistent_hostname("frr1", str(config))

    assert os.stat(config).st_mode & 0o777 == 0o600
