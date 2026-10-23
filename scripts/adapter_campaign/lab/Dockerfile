# checkov:skip=CKV_DOCKER_2:Disposable command runner exits after each bounded stage.
# checkov:skip=CKV_DOCKER_7:BASE_IMAGE is supplied exclusively from digest-pinned images.json.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG LAB_UID=1000
ARG LAB_GID=1000
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
RUN python -m pip install --no-cache-dir uv==0.13.4
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgmp10 libnuma1 libsodium23 libnss3-tools bluez dbus openssl git \
    && rm -rf /var/lib/apt/lists/*
COPY sources /sources
RUN UV_PROJECT_ENVIRONMENT=/opt/medre uv sync --project /sources/medre --locked --extra dev
RUN uv pip install --python /opt/medre/bin/python \
    -e '/sources/matrix[e2e]' -e /sources/meshtastic -e /sources/meshcore \
    -e /sources/lxmf -e /sources/xmpp -e /sources/chatmail -e /sources/simplexchat \
    playwright==1.64.0
RUN UV_PROJECT_ENVIRONMENT=/opt/mmrelay uv sync --project /sources/mmrelay \
    --locked --extra dev --extra test --extra e2e
RUN uv pip install --python /opt/mmrelay/bin/python playwright==1.64.0
ENV XDG_CACHE_HOME=/opt/native-cache PYTHONUNBUFFERED=1
RUN cd /sources/medre && /opt/medre/bin/python -m scripts.adapter_campaign.lab.preload
# Explicit certifi contexts bypass SSL_CERT_FILE. Trust only the disposable
# bench CA in both isolated environments, after network-dependent preloading.
RUN for lab_python in /opt/medre/bin/python /opt/mmrelay/bin/python; do \
    "$lab_python" -c 'import certifi; from pathlib import Path; bundle = Path(certifi.where()); bundle.unlink(); bundle.symlink_to("/lab/config/ca.crt")' || exit 1; \
    done
RUN mkdir -p /opt/native-cache && chown -R ${LAB_UID}:${LAB_GID} /opt/native-cache
RUN chmod -R a+rX /sources
ENV PATH=/opt/medre/bin:$PATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 HOME=/lab/home
USER ${LAB_UID}:${LAB_GID}
WORKDIR /sources/medre
ENTRYPOINT ["/opt/medre/bin/python", "-m", "scripts.adapter_campaign.lab.container"]
