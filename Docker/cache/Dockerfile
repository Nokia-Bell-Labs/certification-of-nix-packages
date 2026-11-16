# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

FROM nixos/nix:2.31.2

RUN cp --remove-destination "$(readlink -f /etc/group)"  /etc/group  && \
    cp --remove-destination "$(readlink -f /etc/passwd)" /etc/passwd

COPY nix-extra.conf /tmp/nix-extra.conf
RUN cat /tmp/nix-extra.conf >> /etc/nix/nix.conf

# Corporate TLS interception: drop .crt files in ./ca/ if needed.
COPY ca/ /usr/local/share/ca-certificates/
RUN if ls /usr/local/share/ca-certificates/*.crt >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/*.crt >> /etc/ssl/certs/ca-bundle.crt && \
        echo "ssl-cert-file = /etc/ssl/certs/ca-bundle.crt" >> /etc/nix/nix.conf ; \
    fi

RUN nix profile install --priority 4 nixpkgs#git nixpkgs#curl nixpkgs#python3
ENV PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:${PATH}"

ARG HARMONIA_REPO=https://github.com/nix-community/harmonia.git
ARG HARMONIA_REV=b14686ad852e280d877577fd1db5e696320b14e5

WORKDIR /build
RUN git clone "$HARMONIA_REPO" harmonia && \
    cd harmonia && git checkout -B cvm-attestation "$HARMONIA_REV"

# Repo: cache/patches/cvm-attestation-v1.patch → same path in the build context.
COPY patches/cvm-attestation-v1.patch /build/patches/cvm-attestation-v1.patch
RUN set -eux; \
    cd /build/harmonia; \
    git apply -p1 /build/patches/cvm-attestation-v1.patch; \
    git -c user.email=build@local -c user.name=build add -A; \
    git -c user.email=build@local -c user.name=build \
        diff --cached --quiet || \
    git -c user.email=build@local -c user.name=build \
        commit -m "cvm attestation patch"; \
    grep -q cvm_attestation_dir harmonia-cache/src/config.rs

RUN cd harmonia && nix build .#harmonia && \
    mkdir -p /opt && \
    cp -aL result /opt/harmonia && rm -rf /build/harmonia

# Fail the image build if the CVM patch was not compiled into harmonia-cache.
RUN grep -aq cvm_attestation_dir /opt/harmonia/bin/harmonia-cache

# Certificate verification shells out to openssl. It is installed here, after the
# Harmonia build, on purpose: a change in a layer above would make Docker compile
# Harmonia again. The build fails here if openssl is not on the PATH.
RUN nix profile install --priority 4 nixpkgs#openssl && openssl version

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY init-cache-store.sh /usr/local/bin/init-cache-store.sh
COPY nar-import-server.py /usr/local/bin/nar-import-server.py
COPY cactee_verify.py /usr/local/bin/cactee_verify.py
COPY cactee-manifest.json /etc/nixtee/cactee-manifest.json
RUN chmod +x /usr/local/bin/entrypoint.sh \
    /usr/local/bin/init-cache-store.sh /usr/local/bin/nar-import-server.py

EXPOSE 5000 5001
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
