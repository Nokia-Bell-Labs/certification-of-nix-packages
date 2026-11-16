#!/usr/bin/env bash
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

# Create an empty Nix chroot store at $CACHE_ROOT (default /certified_store).
# Logical paths stay /nix/store/... ; files live in $CACHE_ROOT/nix/store.
set -euo pipefail
CACHE_ROOT="${CACHE_ROOT:-/certified_store}"
export PATH="/root/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/usr/bin:/bin:${PATH:-}"
mkdir -p "$CACHE_ROOT/nix/store" "$CACHE_ROOT/nix/var/nix/db"
nix --extra-experimental-features nix-command --store "local?root=$CACHE_ROOT" store ping >/dev/null
if [ ! -f "$CACHE_ROOT/nix/var/nix/db/db.sqlite" ]; then
  echo "init-cache-store: db.sqlite was not created under $CACHE_ROOT" >&2
  exit 1
fi
echo "init-cache-store: $CACHE_ROOT (empty served store)"
