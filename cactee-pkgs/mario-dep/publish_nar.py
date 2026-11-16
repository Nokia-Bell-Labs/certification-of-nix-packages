#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Push a nix-store --dump NAR to a Harmonia cache (the Hydra 'nix copy' step).

Harmonia :5000 is read-only. This client PUTs the CACTEE certificate to
:5001/attest/<hash>.json, then the NAR to :5001/import (the cache verifies
the certificate, then the NAR against it, before registering anything).

  python3 publish_nar.py --to http://127.0.0.1:5000 \\
      --store-path /nix/store/<hash>-<name> path.nar \\
      [--cert attestation.json]

Token: HARMONIA_PUBLISH_TOKEN (default nixtee-demo-token).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

STORE_RE = re.compile(r"^/nix/store/[0-9a-z]{32}-[A-Za-z0-9._+-]+$")
HASH_RE = re.compile(r"^[0-9a-z]{32}$")


def hash_part(store_path: str) -> str:
    return Path(store_path).name.split("-", 1)[0]


def import_endpoint(harmonia_to: str) -> str:
    raw = harmonia_to.strip().rstrip("/")
    scheme, rest = raw.split("://", 1)
    hostport, _, _ = rest.partition("/")
    if ":" in hostport and not hostport.startswith("["):
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = hostport, (443 if scheme == "https" else 80)
    if port == 5000:
        hostport = f"{host}:5001"
    return f"{scheme}://{hostport}"


def _put(url: str, data: bytes, headers: dict[str, str], timeout: int = 600) -> str:
    headers = dict(headers)
    headers.setdefault("Content-Length", str(len(data)))
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode().strip()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"{url} HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        hint = "Is the cache import API (:5001) up?"
        if "Broken pipe" in str(exc.reason):
            hint += (
                " If the NAR is large, raise MAX_NAR_BYTES on the cache container "
                "(default 8 GiB in docker-compose; was 512 MiB)."
            )
        raise RuntimeError(f"{url} failed: {exc.reason}. {hint}") from exc


def publish_nar(
    harmonia_to: str,
    nar: Path,
    store_path: str,
    cert: Path | None = None,
    token: str | None = None,
) -> str:
    if not STORE_RE.match(store_path):
        raise ValueError(f"not a store path: {store_path}")
    if not nar.is_file():
        raise FileNotFoundError(nar)
    nar_bytes = nar.stat().st_size
    if nar_bytes > 512 * 1024 * 1024:
        print(
            f"uploading {nar.name}: {nar_bytes / (1024 * 1024):.1f} MiB "
            f"(cache MAX_NAR_BYTES must be >= this)",
            file=sys.stderr,
        )
    token = token or os.environ.get("HARMONIA_PUBLISH_TOKEN", "nixtee-demo-token")
    base = import_endpoint(harmonia_to)
    auth = {"Authorization": f"Bearer {token}"}
    # 1. Certificate first: the cache verifies it (quote, signature,
    #    service info) and keeps it until the NAR arrives.
    if cert is not None:
        raw = cert.read_text(encoding="utf-8")
        json.loads(raw)
        hid = hash_part(store_path)
        if not HASH_RE.match(hid):
            raise ValueError(f"bad hash in {store_path}")
        _put(
            f"{base}/attest/{hid}.json",
            raw.encode(),
            {**auth, "Content-Type": "application/json"},
        )
    # 2. The NAR: the cache checks its sha256 against the certificate
    #    before registering it. Without a certificate it answers 412.
    added = _put(
        f"{base}/import",
        nar.read_bytes(),
        {
            **auth,
            "X-Nix-Store-Path": store_path,
            "Content-Type": "application/octet-stream",
        },
    ).strip()
    return added


def infer_store_path(nar: Path) -> str:
    name = nar.name
    if name.endswith(".nar"):
        name = name[:-4]
    path = f"/nix/store/{name}"
    if not STORE_RE.match(path):
        raise SystemExit(
            f"cannot infer store path from {nar.name}; pass --store-path"
        )
    return path


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Upload a NAR to Harmonia import API (:5001)."
    )
    ap.add_argument("nar", type=Path, help="nix-store --dump file")
    ap.add_argument(
        "--to",
        default=os.environ.get("HARMONIA_URL", "http://127.0.0.1:5000"),
        help="Harmonia read URL (port 5000); import uses 5001",
    )
    ap.add_argument("--store-path", help="/nix/store/<hash>-<name>")
    ap.add_argument("--cert", type=Path, help="optional CVM/CACTEE JSON")
    ap.add_argument(
        "--token",
        default=os.environ.get("HARMONIA_PUBLISH_TOKEN", "nixtee-demo-token"),
    )
    args = ap.parse_args()
    store_path = args.store_path or infer_store_path(args.nar)
    added = publish_nar(args.to, args.nar, store_path, args.cert, args.token)
    print(added)
    print(f"curl -sS {args.to.rstrip('/')}/{hash_part(store_path)}.narinfo")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.exit(str(exc))
