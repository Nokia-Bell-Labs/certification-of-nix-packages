# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

FROM nixos/nix:2.31.2

RUN cp --remove-destination "$(readlink -f /etc/group)"  /etc/group  && \
    cp --remove-destination "$(readlink -f /etc/passwd)" /etc/passwd

COPY nix-extra.conf /tmp/nix-extra.conf
RUN cat /tmp/nix-extra.conf >> /etc/nix/nix.conf

COPY ca/ /usr/local/share/ca-certificates/
RUN if ls /usr/local/share/ca-certificates/*.crt >/dev/null 2>&1; then \
        cat /usr/local/share/ca-certificates/*.crt >> /etc/ssl/certs/ca-bundle.crt && \
        echo "ssl-cert-file = /etc/ssl/certs/ca-bundle.crt" >> /etc/nix/nix.conf ; \
    fi

RUN nix profile install --priority 4 nixpkgs#curl nixpkgs#jq nixpkgs#python3 nixpkgs#docker-client
ENV PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:${PATH}"

COPY publish.sh /usr/local/bin/publish
COPY node-init.sh /usr/local/bin/node-init
RUN chmod +x /usr/local/bin/publish /usr/local/bin/node-init

ENTRYPOINT ["/usr/local/bin/node-init"]
