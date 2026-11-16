#!/usr/bin/env bash
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

# Hydra's last step: put a store path on Harmonia.
# PUTs the CACTEE certificate, then the NAR, to the import API (:5001).
#
# Usage:
#   publish <store-path-or-symlink> [certificate.json]
set -euo pipefail

HARMONIA_URL="${HARMONIA_URL:-http://cache:5000}"
IMPORT_URL="${IMPORT_URL:-http://cache:5001}"
TOKEN="${HARMONIA_PUBLISH_TOKEN:-nixtee-demo-token}"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

[ $# -ge 1 ] || die "usage: publish <store-path> [certificate.json]"

STORE_PATH="$1"
ATTESTATION="${2:-}"

[ -L "$STORE_PATH" ] && STORE_PATH="$(readlink -f "$STORE_PATH")"
case "$STORE_PATH" in
    /nix/store/*) ;;
    *) die "not a store path: $STORE_PATH" ;;
esac
[ -e "$STORE_PATH" ] || die "store path does not exist: $STORE_PATH"

BASE="$(basename "$STORE_PATH")"
HASH="${BASE%%-*}"
[ "${#HASH}" -eq 32 ] || die "unexpected hash length in $BASE"

IMPORT_URL="${IMPORT_URL%/}"

log "Publishing $BASE"
echo "    hash: $HASH"
echo "    import: $IMPORT_URL/import"

NAR="$(mktemp /tmp/publish-XXXXXX.nar)"
trap 'rm -f "$NAR"' EXIT
nix-store --dump "$STORE_PATH" > "$NAR"

# 1. Certificate first: the cache verifies it and keeps it until the NAR arrives.
if [ -n "$ATTESTATION" ]; then
    [ -f "$ATTESTATION" ] || die "certificate not found: $ATTESTATION"
    log "Uploading certificate $HASH.json"
    curl -sS --fail-with-body -X PUT --data-binary @"$ATTESTATION" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        "$IMPORT_URL/attest/$HASH.json"
    echo
else
    warn "no certificate given: the cache refuses the NAR unless REQUIRE_CERTIFICATE=0"
fi

# 2. The NAR: checked against the certificate before it is registered.
log "Uploading NAR"
curl -sS --fail-with-body -X PUT --data-binary @"$NAR" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Nix-Store-Path: $STORE_PATH" \
    -H "Content-Type: application/octet-stream" \
    "$IMPORT_URL/import"

log "Done"
echo "    curl ${HARMONIA_URL%/}/$HASH.narinfo"
