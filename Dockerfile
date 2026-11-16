# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# Named contexts replace these empty stages for local adapter builds.
FROM scratch AS matrix
FROM scratch AS meshtastic
FROM scratch AS meshcore
FROM scratch AS lxmf
FROM scratch AS xmpp
FROM scratch AS chatmail
FROM scratch AS simplexchat

FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS runtime-base
# Native runtime dependencies for the seven supported providers. Keep compilers
# and Git in the builder. Debian security updates are resolved during rebuilds.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgmp10 libnuma1 libsodium23 libnss3-tools bluez ca-certificates \
    && rm -rf /var/lib/apt/lists/*

FROM runtime-base AS builder
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.8
ARG MEDRE_UV_NO_CACHE=0
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_NO_CACHE=${MEDRE_UV_NO_CACHE}
RUN uv venv /opt/venv
COPY . /core
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python /core
ARG MEDRE_ADAPTERS=all
ARG MEDRE_SOURCE=git
ARG MEDRE_SOURCES_DIGEST=none
ARG MEDRE_LOCAL_ADAPTERS=
# Writable bind mounts are disposable build scratch; host contexts are unchanged.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=sources \
    --mount=type=bind,from=matrix,rw,target=/sources/matrix \
    --mount=type=bind,from=meshtastic,rw,target=/sources/meshtastic \
    --mount=type=bind,from=meshcore,rw,target=/sources/meshcore \
    --mount=type=bind,from=lxmf,rw,target=/sources/lxmf \
    --mount=type=bind,from=xmpp,rw,target=/sources/xmpp \
    --mount=type=bind,from=chatmail,rw,target=/sources/chatmail \
    --mount=type=bind,from=simplexchat,rw,target=/sources/simplexchat \
    /opt/venv/bin/python /core/docker/install_adapters.py
RUN /opt/venv/bin/python /core/docker/verify.py --prepare

FROM runtime-base AS runtime
ARG MEDRE_REVISION=unknown
ARG MEDRE_VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/jeremiah-k/medre" \
      org.opencontainers.image.title="MEDRE" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${MEDRE_REVISION}" \
      org.opencontainers.image.version="${MEDRE_VERSION}"
ENV PATH="/opt/venv/bin:${PATH}" \
    MEDRE_HOME=/data HOME=/data \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    SIMPLEX_LIBS_DIR=/opt/simplex-libs
RUN groupadd --gid 1000 medre && useradd --uid 1000 --gid medre --no-create-home medre \
    && mkdir /data && chown medre:medre /data
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/simplex-libs /opt/simplex-libs
COPY --from=builder /opt/medre-build.json /opt/medre-build.json
COPY docker/verify.py /opt/verify-image.py
COPY docker/source_policy.py /opt/source_policy.py
WORKDIR /data
USER 1000:1000
# Generic CLI image: process exit is supervised by Compose. Offline config
# validation is not an honest health check for remote services or radios.
# checkov:skip=CKV_DOCKER_2:Generic CLI image; process supervision is provided by the deployment, not an offline probe.
ENTRYPOINT ["medre"]
CMD ["run"]
