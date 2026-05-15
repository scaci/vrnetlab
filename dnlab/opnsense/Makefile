VENDOR=DNLab
NAME=OPNsense
IMAGE_FORMAT=img
IMAGE_GLOB=OPNsense-*-serial-amd64.img.bz2 OPNsense-*-serial-amd64.img
TAG_SUFFIX?=-dnlab
DONT_COPY_COMMON=1

# Estrai la versione dal filename (es.: OPNsense-26.1.6-serial-amd64.img[.bz2])
VERSION=$(shell echo $(IMAGE) | sed -e 's/^OPNsense-\([0-9.]\+\)-serial-amd64\.img.*$$/\1/')

IMAGE_REPOSITORY?=vrnetlab/dnlab_opnsense

-include ../../makefile-sanity.include
-include ../../makefile.include

# Il Dockerfile usa solo docker/installed.qcow2, quindi non vogliamo che
# il .img.bz2 originale finisca nel build context (~550 MB inutili in piu').
docker-build-image-copy: ;

# Pre-build: copia gli helper di vrnetlab e prepara installed.qcow2
# (decomprime il .bz2 se necessario, converte raw -> qcow2, espande a 8 GB).
docker-pre-build:
	@echo "--> Copying vrnetlab common assets"
	cp ../../common/vrnetlab.py ../../common/healthcheck.py docker/
	@echo "--> Preparing OPNsense disk image (decompress + convert + resize)"
	@if [ "$${IMAGE##*.}" = "bz2" ]; then \
		bunzip2 -kc $(IMAGE) > docker/installed.raw ; \
	else \
		cp $(IMAGE) docker/installed.raw ; \
	fi
	qemu-img convert -f raw -O qcow2 docker/installed.raw docker/installed.qcow2
	qemu-img resize docker/installed.qcow2 8G
	rm -f docker/installed.raw

# Aggiungi un tag aggiuntivo con suffisso "-dnlab" dopo il build standard.
docker-build-extra:
	docker tag $(IMG_REPOSITORY):$(VERSION) $(IMG_REPOSITORY):$(VERSION)$(TAG_SUFFIX)
	@echo "Tagged $(IMG_REPOSITORY):$(VERSION)$(TAG_SUFFIX)"
