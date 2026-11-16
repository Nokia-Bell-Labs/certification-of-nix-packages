#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Generate CACTEE packages that pass examples/dev_test_docker/test_docker.py.

Leaves (no .drv input: bootstrap-tools.tar.xz, busybox):
  take the binary from cache.nixos.org (--drv-source).

Every other node:
  1. look up the output binary on Harmonia
  2. if it is there, use that certified binary
  3. if it is not, copy the .drv from cache.nixos.org and build it
     (inputs: Harmonia first, then cache.nixos.org)

Usage:
  python3 generer_dockerfiles.py graph.dot \\
      -o ./generated \\
      --ca /path/to/ca \\
      --drv-source https://cache.nixos.org \\
      --harmonia http://cache:5000 \\
      --substituter "http://cache:5000 https://cache.nixos.org" \\
      --no-require-sigs
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import defaultdict, deque
from pathlib import Path

EDGE = re.compile(r'^"([^"]+)"\s*->\s*"([^"]+)"')
NODE = re.compile(r'^"([^"]+)"\s*\[label')

BASE_IMAGE = (
    "nixos/nix@sha256:"
    "377d4887aca98f0dfa12971c1ea6d6a625a435d8b610d4c95a436843da6fbfd1"
)
DEFAULT_SUBSTITUTER = "https://cache.nixos.org"
DEFAULT_TRUSTED_KEY = (
    "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
)

DOCKERFILE_HEAD = """FROM {image}

ARG http_proxy
ARG https_proxy
ENV http_proxy=$http_proxy
ENV https_proxy=$https_proxy
ENV HTTP_PROXY=$http_proxy
ENV HTTPS_PROXY=$https_proxy

# --- CA Zscaler  ---
COPY ca/ /usr/local/share/ca-certificates/
RUN cat /usr/local/share/ca-certificates/zscaler-cert-*.crt >> /etc/ssl/certs/ca-bundle.crt
RUN echo "ssl-cert-file = /etc/ssl/certs/ca-bundle.crt" >> /etc/nix/nix.conf
RUN echo "extra-experimental-features = nix-command" >> /etc/nix/nix.conf
RUN echo "substituters = {substituter}" >> /etc/nix/nix.conf
RUN echo "{nix_sig_line}" >> /etc/nix/nix.conf

# --- Contrat CACTEE ---
RUN mkdir -p /tmp/inputs /tmp/outputs

ARG DRV=/nix/store/{drv}
"""

# Leaf: no .drv input. Binary comes from cache.nixos.org (fixed-output fetch).
DOCKERFILE_LEAF = DOCKERFILE_HEAD + """
# --- Leaf: binary from cache.nixos.org, not Harmonia ---
RUN nix --extra-experimental-features nix-command copy --from "{drv_source}" {copy_flags} "$DRV"
RUN nix-store --realise --option substituters "{drv_source}" "$DRV"
RUN echo "leaf {drv_source}" > /tmp/inputs-used
RUN nix-store -q --outputs "$DRV" > /tmp/outpaths && cat /tmp/outpaths
CMD ["sh", "-c", "set -e; while read p; do nix-store --dump \\"$p\\" > /tmp/outputs/$(basename \\"$p\\").nar; done < /tmp/outpaths; cp /tmp/inputs-used /tmp/outputs/inputs-used.txt; ls -l /tmp/outputs"]
"""

# Non-leaf: Harmonia first for THIS output; miss => fetch .drv and build.
DOCKERFILE_NODE = DOCKERFILE_HEAD + """
# --- Recipe (only needed if Harmonia misses) ---
RUN nix --extra-experimental-features nix-command copy --from "{drv_source}" {copy_flags} "$DRV"

# --- Harmonia first; build from .drv only on miss ---
RUN outs="$(nix-store -q --outputs "$DRV")" && \\
    if nix --extra-experimental-features nix-command copy --from "{harmonia}" {copy_flags} $outs; then \\
      echo "harmonia hit: certified binary" > /tmp/inputs-used; \\
      echo "$outs" >> /tmp/inputs-used; \\
    else \\
      echo "harmonia miss: building from .drv"; \\
      refs="$(nix-store -q --references "$DRV" | grep '\\.drv$' || true)"; \\
      if [ -n "$refs" ]; then nix-store --realise $refs; fi; \\
      nix-store --realise --option substituters "" "$DRV"; \\
      : > /tmp/inputs-used; \\
      for d in $refs; do nix-store -q --outputs "$d" >> /tmp/inputs-used; done; \\
      sort -u -o /tmp/inputs-used /tmp/inputs-used; \\
      echo "built locally; inputs:" >> /tmp/inputs-used; \\
    fi; \\
    echo "inputs-used:"; cat /tmp/inputs-used

RUN nix-store -q --outputs "$DRV" > /tmp/outpaths && cat /tmp/outpaths
CMD ["sh", "-c", "set -e; while read p; do nix-store --dump \\"$p\\" > /tmp/outputs/$(basename \\"$p\\").nar; done < /tmp/outpaths; cp /tmp/inputs-used /tmp/outputs/inputs-used.txt; ls -l /tmp/outputs"]
"""


def pkg_name(drv: str) -> str:
    n = drv.split("-", 1)[1] if "-" in drv else drv
    return n[:-4] if n.endswith(".drv") else n


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def hash8(drv: str) -> str:
    return drv.split("-", 1)[0][:8]


def is_leaf(drv: str, inputs: dict[str, list[str]]) -> bool:
    return not any(i.endswith(".drv") for i in inputs.get(drv, []))


def load_dot(path: Path) -> tuple[dict[str, str], dict[str, list[str]], dict[str, list[str]]]:
    labels: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = NODE.match(line.strip())
        if m:
            nid = m.group(1)
            if nid.endswith(".drv"):
                labels[nid] = nid
    inputs: dict[str, list[str]] = defaultdict(list)
    users: dict[str, list[str]] = defaultdict(list)
    for line in text.splitlines():
        m = EDGE.match(line.strip())
        if not m:
            continue
        src, dst = m.group(1), m.group(2)
        if src in labels and dst in labels:
            if src not in inputs[dst]:
                inputs[dst].append(src)
            if dst not in users[src]:
                users[src].append(dst)
    return labels, inputs, users


def find_sinks(labels: dict[str, str], users: dict[str, list[str]]) -> list[str]:
    """Nodes nothing else depends on — usually the package the graph was built for."""
    return sorted(n for n in labels if not users.get(n))


def unfold(
    root: str,
    inputs: dict[str, list[str]],
    max_depth: int,
) -> tuple[list[tuple[str, int]], dict[str, int], dict[str, int]]:
    seen: set[tuple[str, int]] = {(root, 0)}
    queue: deque[tuple[str, int]] = deque([(root, 0)])
    inst: list[tuple[str, int]] = []
    min_layer: dict[str, int] = {}
    max_layer: dict[str, int] = {}
    while queue:
        nid, depth = queue.popleft()
        inst.append((nid, depth))
        min_layer[nid] = min(min_layer.get(nid, depth), depth)
        max_layer[nid] = max(max_layer.get(nid, depth), depth)
        if depth >= max_depth:
            continue
        for inp in inputs.get(nid, []):
            key = (inp, depth + 1)
            if key not in seen:
                seen.add(key)
                queue.append(key)
    return inst, min_layer, max_layer


def order_bottom_up(inst: list[tuple[str, int]]) -> tuple[list[str], int]:
    by_depth: dict[int, list[str]] = defaultdict(list)
    for nid, depth in inst:
        by_depth[depth].append(nid)
    n_layers = 1 + max(by_depth)
    seen: set[str] = set()
    ordered: list[str] = []
    for depth in range(n_layers - 1, -1, -1):
        layer = sorted(set(by_depth[depth]), key=lambda d: (pkg_name(d), d))
        for nid in layer:
            if nid in seen:
                continue
            seen.add(nid)
            ordered.append(nid)
    return ordered, n_layers


def write_dockerfile(
    path: Path,
    drv: str,
    substituter: str,
    nix_sig_line: str,
    copy_flags: str,
    drv_source: str,
    harmonia: str,
    leaf: bool,
) -> None:
    flags = copy_flags.strip()
    tmpl = DOCKERFILE_LEAF if leaf else DOCKERFILE_NODE
    path.write_text(
        tmpl.format(
            image=BASE_IMAGE,
            drv=drv,
            substituter=substituter,
            drv_source=drv_source,
            harmonia=harmonia,
            nix_sig_line=nix_sig_line,
            copy_flags=flags,
        ),
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate one CACTEE package per unique .drv, bottom-up from a Graphviz .dot."
    )
    ap.add_argument("dot", type=Path, help="Graphviz .dot of the .drv graph")
    ap.add_argument(
        "-o",
        "--outdir",
        type=Path,
        required=True,
        help="folder where generated packages are written",
    )
    ap.add_argument(
        "--root",
        default=None,
        help="root .drv (the package the graph was built for). "
        "If omitted, uses the unique node with no dependents",
    )
    ap.add_argument(
        "--ca",
        type=Path,
        required=True,
        help="folder with zscaler-cert-*.crt (required by test_docker.py COPY ca/)",
    )
    ap.add_argument(
        "--substituter",
        default=DEFAULT_SUBSTITUTER,
        help="caches for input binaries when building on a Harmonia miss, "
        "space separated; Harmonia first, cache.nixos.org last "
        f"(default: {DEFAULT_SUBSTITUTER})",
    )
    ap.add_argument(
        "--drv-source",
        default=DEFAULT_SUBSTITUTER,
        help="cache for .drv recipes and for leaf binaries "
        f"(default: {DEFAULT_SUBSTITUTER})",
    )
    ap.add_argument(
        "--harmonia",
        default=None,
        help="certified binary cache checked before any non-leaf build "
        "(default: first URL in --substituter)",
    )
    ap.add_argument(
        "--trusted-key",
        default=DEFAULT_TRUSTED_KEY,
        help="trusted-public-keys entry for the substituter "
        "(ignored with --no-require-sigs)",
    )
    ap.add_argument(
        "--no-require-sigs",
        action="store_true",
        help="set require-sigs = false (unsigned Harmonia / local cache)",
    )
    ap.add_argument("--max-depth", type=int, default=80)
    ap.add_argument("--limit", type=int, help="generate only the first N packages (bottom-up)")
    args = ap.parse_args()

    dot = args.dot.expanduser()
    outdir = args.outdir.expanduser()
    ca_dir = args.ca.expanduser()
    substituter = args.substituter.strip()
    drv_source = args.drv_source.strip()
    harmonia = (args.harmonia or substituter.split()[0]).strip()

    if not dot.is_file():
        sys.exit(f"Missing DOT file: {dot}")

    if not ca_dir.is_dir():
        sys.exit(f"Missing --ca directory: {ca_dir}")
    certs = list(ca_dir.glob("zscaler-cert-*.crt"))
    if not certs:
        sys.exit(f"No zscaler-cert-*.crt in {ca_dir}")

    if args.no_require_sigs:
        nix_sig_line = "require-sigs = false"
        copy_flags = "--no-check-sigs"
    else:
        nix_sig_line = f"trusted-public-keys = {args.trusted_key}"
        copy_flags = ""

    labels, inputs, users = load_dot(dot)
    if not labels:
        sys.exit(f"No .drv nodes in DOT: {dot}")

    root = args.root
    if root is None:
        sinks = find_sinks(labels, users)
        if len(sinks) == 1:
            root = sinks[0]
            print(f"using root: {root}", file=sys.stderr)
        elif not sinks:
            sys.exit("No root found (every .drv has dependents). Pass --root.")
        else:
            preview = "\n".join(f"  {s}" for s in sinks[:20])
            extra = "" if len(sinks) <= 20 else f"\n  ... ({len(sinks)} total)"
            sys.exit(
                f"Several possible roots (nodes with no dependents). Pass --root.\n"
                f"{preview}{extra}"
            )
    if root not in labels:
        sys.exit(f"Root not found in DOT: {root}")

    inst, _min_layer, max_layer = unfold(root, inputs, args.max_depth)
    ordered, n_layers = order_bottom_up(inst)
    if args.limit:
        ordered = ordered[: args.limit]
    skipped_copies = len(inst) - len(set(n for n, _d in inst))
    bottom = n_layers - 1
    root_name = pkg_name(root)

    try:
        outdir.mkdir(parents=True, exist_ok=True)
        probe = outdir / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        sys.exit(
            f"Cannot write to {outdir}: {e}\n"
            "The folder exists but is not writable (often created from Windows).\n"
            "In WSL, recreate it as your user:\n"
            f"  rm -rf {outdir}\n"
            f"  mkdir -p {outdir}"
        )
    generated = []
    n_leaves = 0

    for idx, drv in enumerate(ordered, 1):
        name = pkg_name(drv)
        folder_name = f"{idx:03d}-{slug(name)}-{hash8(drv)}"
        folder = outdir / folder_name
        code = folder / "code"
        ca_dst = code / "ca"
        if ca_dst.exists():
            shutil.rmtree(ca_dst)
        code.mkdir(parents=True, exist_ok=True)
        leaf = is_leaf(drv, inputs)
        if leaf:
            n_leaves += 1
        node_subs = drv_source if leaf else substituter
        write_dockerfile(
            code / "Dockerfile",
            drv,
            node_subs,
            nix_sig_line,
            copy_flags,
            drv_source,
            harmonia,
            leaf,
        )
        shutil.copytree(ca_dir, ca_dst)

        config = {
            "name": f"nix/{slug(name)}-{hash8(drv)}",
            "inputs": [],
            "code_folder": "code/",
        }
        (folder / "config.json").write_text(
            json.dumps(config, indent=4) + "\n", encoding="utf-8", newline="\n"
        )
        generated.append((idx, drv, name, folder_name, leaf))

    order_path = outdir / "00-build-order.txt"
    with order_path.open("w", encoding="utf-8", newline="\n") as fh:
        cur = None
        for idx, drv, name, folder_name, leaf in generated:
            layer = max_layer[drv]
            if layer != cur:
                cur = layer
                fh.write(f"\n# ===== HTML L{layer} (from the bottom) =====\n")
            kind = "leaf" if leaf else "node"
            fh.write(f"{idx:03d}  L{layer}  {kind}  /nix/store/{drv}\n")

    print(f"HTML instances            : {len(inst)}")
    print(f"packages generated        : {len(generated)}")
    print(f"leaves (cache.nixos.org)  : {n_leaves}")
    print(f"nodes (Harmonia then .drv): {len(generated) - n_leaves}")
    print(f"unique hashes skipped copy: {skipped_copies}")
    print(f"HTML layers               : 0 ({root_name}) .. {bottom} (bottom)")
    print(f"root                      : {root}")
    print(f"ca                        : {ca_dir}")
    print(f"harmonia                  : {harmonia}")
    print(f"drv source / leaf binaries: {drv_source}")
    print(f"build-input substituters  : {substituter}")
    print(f"nix.conf sig              : {nix_sig_line}")
    print(f"first (bottom)            : {', '.join(g[2] for g in generated[:8])}")
    print(f"last (top)                : {', '.join(g[2] for g in generated[-5:])}")
    print(f"written to {outdir}/")
    print()
    print("test_docker.py example:")
    print(f"  python3 test_docker.py {outdir / generated[0][3] / 'config.json'}")


if __name__ == "__main__":
    main()
