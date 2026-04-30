# ------------------------------------------------------------------------------
# NVIDIA Cumulus VX - vrnetlab / srl-labs Makefile
#
# Place the Cumulus VX qcow2 image in this directory, then run:
#
#   make
#
# The resulting image will be tagged:
#   vrnetlab/nvidia_cumulusvx:<VERSION>
# ------------------------------------------------------------------------------

VENDOR       = NVIDIA
NAME         = CumulusVX

IMAGE_FORMAT = qcow2
IMAGE_GLOB   = *.qcow2

# Common NVIDIA filenames look like:
#   cumulus-linux-5.10.0-vx-amd64-qemu.qcow2
#   cumulus-vx-4.4.5.qcow2
VERSION      = $(shell echo $(IMAGE) \
                 | sed -e 's/\.$(IMAGE_FORMAT)$$//' \
                       -e 's/^cumulus-linux-//' \
                       -e 's/^cumulus-vx-//' \
                       -e 's/-vx-amd64-qemu$$//' \
                       -e 's/-amd64-qemu$$//')

-include ../../makefile-sanity.include
-include ../../makefile.include
