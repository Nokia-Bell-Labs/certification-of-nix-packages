#!/usr/bin/env bash
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

# Publisher / client: optional substituter config, then run the container command.
set -euo pipefail

export PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/usr/bin:/bin:${PATH:-}"
# The volume on /nix hides the store paths this binary was linked against.
# Those paths were copied under /opt at image build; put them back before nix runs.
if [ -d /opt/fork-closure/nix-store ]; then
    mkdir -p /nix/store
    for item in /opt/fork-closure/nix-store/*; do
        [ -e "$item" ] || continue
        name=$(basename "$item")
        if [ ! -e "/nix/store/$name" ]; then
            cp -a "$item" "/nix/store/$name"
        fi
    done
fi

if [ -x /opt/nix-fork/bin/nix ]; then
    export PATH="/opt/nix-fork/bin:${PATH}"
    export LD_LIBRARY_PATH="/opt/nix-fork/lib:/opt/nix-fork/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

# Volume /nix hides python3 baked into the image. Install into THIS store.
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 missing on /nix volume — nix profile install nixpkgs#python3"
    nix profile install --priority 4 nixpkgs#python3 nixpkgs#curl nixpkgs#jq
fi

# Only the client points at the cache; the publisher builds from scratch.
if [ -n "${SUBSTITUTERS:-}" ]; then
    mkdir -p /root/.config/nix
    {
        echo "experimental-features = nix-command flakes"
        echo "substituters = $SUBSTITUTERS"
        echo "require-sigs = false"
    } > /root/.config/nix/nix.conf
    echo "substituters: $SUBSTITUTERS"
fi

exec "$@"
