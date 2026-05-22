FROM public.ecr.aws/docker/library/debian:13-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG FRR_VERSION=10.6.1
ENV VERSION=${FRR_VERSION}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update -qy \
    && apt-get install -y --no-install-recommends \
      bridge-utils \
      dnsutils \
      iproute2 \
      iptables \
      inetutils-ping \
      nftables \
      python3 \
      python3-zombie-telnetlib \
      qemu-kvm \
      qemu-utils \
      socat \
      ssh \
      tcpdump \
      telnet \
    && rm -rf /var/lib/apt/lists/*

COPY installed.qcow2 /installed.qcow2
COPY vmlinuz /vmlinuz
COPY initrd.img /initrd.img
COPY launch.py /launch.py
COPY vrnetlab.py /vrnetlab.py
COPY healthcheck.py /healthcheck.py

RUN chmod 0755 /launch.py /healthcheck.py

EXPOSE 5000 10000-10099

HEALTHCHECK CMD ["/healthcheck.py"]
ENTRYPOINT ["/launch.py"]
