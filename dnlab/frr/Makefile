VENDOR=DNLab
NAME=FRR
VERSION?=10.6.1
TAG_SUFFIX?=-dnlab
IMAGE_REPOSITORY?=vrnetlab/dnlab_frr
IMAGE_TAG=$(VERSION)$(TAG_SUFFIX)
DOCKER_DIR=docker

ifdef DOCKER_PLATFORM
DOCKER_BUILD_CMD=docker buildx build --platform $(DOCKER_PLATFORM)
else
DOCKER_BUILD_CMD=docker build
endif

.PHONY: all build docker-image docker-push version-test usage

all: docker-image

build: docker-image

docker-image:
	@echo "Building docker image $(IMAGE_REPOSITORY):$(IMAGE_TAG)"
	$(DOCKER_BUILD_CMD) \
		--build-arg FRR_VERSION=$(VERSION) \
		--build-arg http_proxy=$(http_proxy) \
		--build-arg HTTP_PROXY=$(HTTP_PROXY) \
		--build-arg https_proxy=$(https_proxy) \
		--build-arg HTTPS_PROXY=$(HTTPS_PROXY) \
		-t $(IMAGE_REPOSITORY):$(IMAGE_TAG) \
		$(DOCKER_DIR)

docker-push:
	docker push $(IMAGE_REPOSITORY):$(IMAGE_TAG)

version-test:
	@echo Version: $(VERSION)
	@echo Image: $(IMAGE_REPOSITORY):$(IMAGE_TAG)

usage:
	@echo "Usage:"
	@echo "  make"
	@echo "  make VERSION=10.6.1"
	@echo "  make IMAGE_REPOSITORY=vrnetlab/dnlab_frr VERSION=10.6.1"
	@echo "  make TAG_SUFFIX=        # build a non-dnlab tag intentionally"
