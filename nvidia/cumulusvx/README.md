# NVIDIA Cumulus VX

This directory builds a vrnetlab container image for NVIDIA Cumulus VX qcow2
images.

## Build

Copy the Cumulus VX qcow2 image into this directory and run:

```bash
make
```

The image is tagged as:

```text
vrnetlab/nvidia_cumulusvx:<version>
```

## DnLab / containerlab

Cumulus VX should be deployed as a `generic_vm` node:

```yaml
kind: generic_vm
image: vrnetlab/nvidia_cumulusvx:<version>
env:
  CLAB_MGMT_PASSTHROUGH: "true"
  CLAB_MGMT_DHCP: "true"
```

The launcher intentionally boots the appliance in factory mode. It does not
inject configuration, does not create a cloud-init/config-drive ISO, and only
waits for a console login prompt before marking the VM as running.

If `/config` is mounted by the orchestrator, the launcher creates a persistent
qcow2 overlay there and uses it as the writable disk.
