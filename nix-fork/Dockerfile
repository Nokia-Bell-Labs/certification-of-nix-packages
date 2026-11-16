# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

# Publisher image: stock nixos/nix plus the Nix fork.
# The fork is upstream NixOS/nix at NIX_REV with the patches in
# nix-src-patches/ applied in order. The nix-src/ folder next to this file is
# not used (see .dockerignore).
FROM nixos/nix:2.31.2

ARG NIX_REPO=https://github.com/NixOS/nix.git
ARG NIX_REV=0765ffa540e2adfda7b69efafdbac821ebc48034

RUN cp --remove-destination "$(readlink -f /etc/group)" /etc/group && \
    cp --remove-destination "$(readlink -f /etc/passwd)" /etc/passwd

COPY nix-extra.conf /tmp/nix-extra.conf
RUN cat /tmp/nix-extra.conf >> /etc/nix/nix.conf

COPY ca/ /usr/local/share/ca-certificates/
RUN if ls /usr/local/share/ca-certificates/*.crt >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/*.crt >> /etc/ssl/certs/ca-bundle.crt && \
        echo "ssl-cert-file = /etc/ssl/certs/ca-bundle.crt" >> /etc/nix/nix.conf ; \
    fi
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt
ENV NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt
ENV GIT_SSL_CAINFO=/etc/ssl/certs/ca-bundle.crt

RUN nix profile install --priority 4 \
        nixpkgs#curl nixpkgs#jq nixpkgs#python3 nixpkgs#docker-client
ENV PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:${PATH}"

# Upstream source at the pinned commit, then the fork patches (0001, 0002, 0003).
# Only 0001 is needed to compile; 0002 and 0003 keep /nix-src the full fork tree.
# --index stages the patched files: nix-shell (flake-compat) reads /nix-src as
# a git tree and leaves out untracked files, such as the ones 0001 adds.
COPY nix-src-patches/*.patch /nix-src-patches/
RUN git init -q /nix-src \
 && cd /nix-src \
 && git fetch -q --depth 1 "$NIX_REPO" "$NIX_REV" \
 && git -c advice.detachedHead=false checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$NIX_REV" \
 && git apply --index --whitespace=nowarn /nix-src-patches/*.patch \
 && echo "nix-src: $NIX_REV + $(ls /nix-src-patches | tr '\n' ' ')"

RUN cd /nix-src \
 && ( nix-shell --quiet --run 'meson setup build --prefix=/opt/nix-fork -Dunit-tests=false -Dfunctional-tests=false -Djson-schema-checks=false && ninja -C build && ninja -C build install' > /tmp/nix-build.log 2>&1 \
     || { tail -n 120 /tmp/nix-build.log; exit 1; } )

COPY seed-fork-store.py /tmp/seed-fork-store.py
RUN python3 /tmp/seed-fork-store.py && rm /tmp/seed-fork-store.py

ENV PATH="/opt/nix-fork/bin:/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/nix-fork/lib:/opt/nix-fork/lib/x86_64-linux-gnu"

COPY publish.sh /usr/local/bin/publish
COPY node-init.sh /usr/local/bin/node-init
RUN printf '\nrequire-attestation = false\n' >> /etc/nix/nix.conf \
 && python3 -c "from pathlib import Path; \
[Path(p).write_bytes(Path(p).read_bytes().replace(b'\r\n', b'\n')) for p in ('/usr/local/bin/publish', '/usr/local/bin/node-init')]" \
 && chmod +x /usr/local/bin/publish /usr/local/bin/node-init \
 && LD_LIBRARY_PATH="/opt/nix-fork/lib:/opt/nix-fork/lib/x86_64-linux-gnu" /opt/nix-fork/bin/nix --version

ENTRYPOINT ["/usr/local/bin/node-init"]
