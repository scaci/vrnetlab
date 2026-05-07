FROM public.ecr.aws/docker/library/debian:13-slim

ARG DEBIAN_FRONTEND=noninteractive
ARG FRR_VERSION=10.6.1
ARG FRR_DEB_VERSION=${FRR_VERSION}-0~deb13u1

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update -qy \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      gnupg \
      iproute2 \
      iputils-ping \
      less \
      lsb-release \
      mount \
      procps \
      tcpdump \
      telnet \
      tini \
      vim-tiny \
    && curl -fsSL https://deb.frrouting.org/frr/keys.gpg \
      -o /usr/share/keyrings/frrouting.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr trixie frr-10.6" \
      > /etc/apt/sources.list.d/frr.list \
    && apt-get update -qy \
    && apt-get install -y --no-install-recommends \
      "frr=${FRR_DEB_VERSION}" \
      "frr-pythontools=${FRR_DEB_VERSION}" \
    && apt-mark hold frr frr-pythontools \
    && rm -rf /var/lib/apt/lists/*

COPY daemons /etc/frr/daemons
COPY frr.conf /etc/frr/frr.conf
COPY entrypoint.sh /usr/local/sbin/dnlab-frr-entrypoint

RUN chmod 0640 /etc/frr/daemons /etc/frr/frr.conf \
    && chown frr:frr /etc/frr/daemons /etc/frr/frr.conf \
    && chmod 0755 /usr/local/sbin/dnlab-frr-entrypoint

EXPOSE 2601 2602 2603 2604 2605 2606 2607 2608 2609 2610 2611 2612 2613 2614 2615 2616 2617 2618

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/sbin/dnlab-frr-entrypoint"]
CMD []
