#!/usr/bin/env bash
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

# Cache node: Harmonia read :5000, token-gated NAR import :5001, Cvm* narinfos.
set -euo pipefail

BIND="${BIND:-0.0.0.0:5000}"
WORKERS="${WORKERS:-2}"
ATTESTATION_DIR="${ATTESTATION_DIR:-/var/lib/nix-certification}"
CACHE_ROOT="${CACHE_ROOT:-/certified_store}"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; exit 1; }

mkdir -p "$ATTESTATION_DIR"

log "Initialising empty served store at $CACHE_ROOT"
/usr/local/bin/init-cache-store.sh

cat > /etc/harmonia.toml <<EOF
bind = "$BIND"
workers = $WORKERS
max_connection_rate = 256
priority = 30
virtual_nix_store = "/nix/store"
real_nix_store = "$CACHE_ROOT/nix/store"
nix_db_path = "$CACHE_ROOT/nix/var/nix/db/db.sqlite"
cvm_attestation_dir = "$ATTESTATION_DIR"
EOF

log "Starting nar-import-server on ${PUBLISH_BIND:-0.0.0.0:5001}"
python3 /usr/local/bin/nar-import-server.py &
IMPORT_PID=$!

log "Starting harmonia-cache on $BIND"
echo "    attestations dir: $ATTESTATION_DIR"
echo "    served store:     $CACHE_ROOT/nix/store"
echo
env CONFIG_FILE=/etc/harmonia.toml /opt/harmonia/bin/harmonia-cache &
CACHE_PID=$!

if wait -n "$IMPORT_PID" "$CACHE_PID"; then
  status=0
else
  status=$?
fi
if [ "$status" -ne 0 ]; then
  die "nar-import (pid $IMPORT_PID) or harmonia-cache (pid $CACHE_PID) exited with status $status"
fi
kill "$IMPORT_PID" "$CACHE_PID" 2>/dev/null || true
wait || true
exit 0
