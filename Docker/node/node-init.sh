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

# Client: verify CACTEE certificates in this process, not via the stock daemon.
# The manifest below is only for CACTEE_AZURE_SIMULATE=1. Production installs
# /etc/nix/cactee-manifest.json out of band and this script must not replace it.
# Its MRENCLAVE and service hashes must equal CACTEE_MRENCLAVE / CACTEE_SERVICE_* in
# docker-compose.yml and Docker/cache/cactee-manifest.json, or every certificate is refused.
if [ "${VERIFY_ATTESTATION:-}" = "1" ]; then
    unset NIX_REMOTE
    conf=/etc/nix/nix.conf
    tmp=$(mktemp)
    grep -v -e '^require-attestation' -e '^attestation-cactee-manifest' -e '^attestation-store' "$conf" > "$tmp" || true
    cat >> "$tmp" <<'EOF'
require-attestation = true
attestation-cactee-manifest = /etc/nix/cactee-manifest.json
attestation-store = /var/lib/nix-attestations
EOF
    cat "$tmp" > "$conf"
    rm -f "$tmp"
    mkdir -p /var/lib/nix-attestations
    if [ "${CACTEE_AZURE_SIMULATE:-}" = "1" ]; then
    cat > /etc/nix/cactee-manifest.json <<'EOF'
{
  "EXPECTED_DUET_ADMIN_MRENCLAVE": "9cd38b0f69562e81f5ae50e1a57b79b454f7f68595a7d4e5e34b6c75c4d154c8",
  "EXPECTED_DUET_SERVICE_INFO": {
    "service_code_hash": "b853d36744e9ca4b7105b25c5e3dd26d91c99f166bce1ed8264bbf77eaefe683",
    "service_tools_hashes": {
      "huggingface": "86e3b11ab5475b83ada1f34cae1089aff14eb236e3814fbb42228d517a5c8bb9",
      "build_tools.py": "95fb3d065af5c8c1b25e0cb22a76c193cafe57158c7eee85849bc1c94bf8f4ab"
    },
    "service_dependencies_hash": {
      "cpu": "91aa92dc4e204aead93fd0797c991dab8fb8b198138e4ad3dc803e350a809d2b",
      "gpu": "06e8368f5451b021c83ac2b41ceac201a5bcdba3193377d84a7c0b0ffffb1e29"
    }
  }
}
EOF
        echo "fork nix: CACTEE verification on, Azure simulated, NAR sha256 is checked"
    else
        echo "fork nix: CACTEE verification on, using the installed cactee-manifest.json"
    fi
fi

exec "$@"
