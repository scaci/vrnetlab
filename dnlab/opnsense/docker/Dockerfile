FROM public.ecr.aws/docker/library/debian:bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG VERSION
ENV VERSION=$VERSION

RUN apt-get update -qy \
 && apt-get install -y --no-install-recommends \
    bridge-utils \
    iproute2 \
    socat \
    qemu-kvm \
    qemu-utils \
    tcpdump \
    ssh \
    inetutils-ping \
    dnsutils \
    iptables \
    nftables \
    telnet \
    python3 \
    genisoimage \
 && rm -rf /var/lib/apt/lists/*

COPY installed.qcow2 /installed.qcow2
COPY --chmod=0755 *.py /

EXPOSE 22 5000 10000-10099
HEALTHCHECK CMD ["/healthcheck.py"]
ENTRYPOINT ["/launch.py"]
