#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Generate complete CACTEE package dirs for test_docker.py / orchestration.

Each package contains config.json, code/ (Dockerfile + ca/), and inputs/.
The orchestrator POSTs these dirs as-is; it does not rewrite config or inputs.

Leaves (no .drv input: tarballs, patches, busybox, …):
  bundle the leaf .drv closure into inputs/ as one <output>.export, named
  after the leaf's output (e.g. 2pizl7lq…-bootstrap-tools.tar.xz.export)
  (host needs nix; paths are prefetched from --drv-source). config.json
  declares inputs/ for CACTEE, which mounts it at /tmp/inputs at run only.
  The leaf CMD imports it — no substituter / no network in the run — then
  CHECKS the output against the hash pinned in the leaf's own .drv (flat or
  recursive, sha1/256/512) and fails on mismatch, so the certificate attests
  a verified copy. Nothing leaf-specific is baked into the image.

Every other node:
  bundle the node's RECIPE (the .drv closure: this .drv, its input .drvs and
  declared sources, no binaries) into inputs/ as <hash>-<name>.drv.export. It
  is the node's only CACTEE input, so the certificate says which recipe was
  executed. The Dockerfile copies the input binaries (code/input-paths.txt)
  from Harmonia at build; the run phase imports the recipe and builds with no
  substituter (cert-aware Nix verification of those inputs is future work).
  The orchestrator never copies NARs or certificates into node packages.

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
import os
import re
import shutil
import stat
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path

EDGE = re.compile(r'^"([^"]+)"\s*->\s*"([^"]+)"')
NODE = re.compile(r'^"([^"]+)"\s*\[label')

BASE_IMAGE = "nixtee-publisher:fork"
DEFAULT_SUBSTITUTER = "https://cache.nixos.org"
DEFAULT_TRUSTED_KEY = (
    "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
)

# Run-phase scripts. CACTEE builds the image from code/ (network on) and then
# runs the container with network_disabled=True: whatever the container does
# at run lives in one of these two files, copied into code/ by the generator
# and called by the Dockerfile's CMD. Plain POSIX sh: they run under the
# nixos/nix image's sh, which has coreutils and nix but no sed.

RUN_LEAF_SH = r"""#!/bin/sh
# run-leaf.sh -- CACTEE run phase for a LEAF (fixed-output derivation).
#
# Network is off. The leaf's closure arrives only through the CACTEE input
# /tmp/inputs/$LEAF_EXPORT (a nix-store --export). The script imports it,
# checks the output against the hash pinned in the leaf's own .drv, and only
# then dumps the output NAR that CACTEE certifies.
#
# Environment (set by the Dockerfile):
#   DRV           /nix/store/<hash>-<name>.drv       the leaf's recipe
#   LEAF_OUTPUTS  /nix/store/<hash>-<name>           its single output
#   LEAF_EXPORT   <hash>-<name>.export               the CACTEE input file
set -e

# 0. nixos/nix is multi-user: nix-store needs the daemon, which nothing starts
#    in a plain `docker run`.
if ! [ -S /nix/var/nix/daemon-socket/socket ] 2>/dev/null; then
    nix-daemon --daemon 2>/tmp/nix-daemon.log &
    for _ in $(seq 1 60); do
        [ -S /nix/var/nix/daemon-socket/socket ] 2>/dev/null && break
        sleep 0.1
    done
fi
export NIX_REMOTE=daemon

# 1. Import the closure received from CACTEE (required).
le="/tmp/inputs/$LEAF_EXPORT"
[ -f "$le" ] || { echo "leaf: $le missing (CACTEE input not provided)"; exit 1; }
nix-store --import < "$le"
test -e "$DRV"

# 2. Read the expected hash from the .drv itself, in its ATerm form:
#      Derive([("out","/nix/store/...","sha256","<hex>")],...
#    "r:sha256" means a recursive (NAR) hash, plain "sha256" a flat file hash.
#    `nix derivation show` is not used: its JSON changed across Nix versions.
d=$(head -c 4096 "$DRV")
case "$d" in
    'Derive([("out","'*) ;;
    *) echo "leaf check: $DRV is not a single-output derivation"; exit 1 ;;
esac
d=${d#'Derive([("out","'}
lp=${d%%\"*}; d=${d#*\",\"}     # declared output path
la=${d%%\"*}; d=${d#*\",\"}     # hash algorithm (sha256, r:sha256, ...)
lw=${d%%\"*}                    # expected hash, base16

# 3. Refuse anything unexpected.
case "$lw" in
    ''|*[!0-9a-f]*) echo "leaf check: $DRV has no fixed-output hash"; exit 1 ;;
esac
lm=flat
case "$la" in
    r:*)    lm=nar; la=${la#r:} ;;
    text:*) echo "leaf check: text hash mode unsupported"; exit 1 ;;
esac
case "$la" in
    sha1|sha256|sha512) ;;
    *) echo "leaf check: unsupported hash algo $la"; exit 1 ;;
esac
set -- $LEAF_OUTPUTS
[ "$#" = 1 ] && [ "$1" = "$lp" ] || {
    echo "leaf check: LEAF_OUTPUTS=$LEAF_OUTPUTS but $DRV declares $lp"; exit 1; }

# 4. Recompute the hash of what was imported and compare.
if [ "$lm" = flat ]; then
    lg=$("${la}sum" "$lp" | cut -d" " -f1)
else
    lg=$(nix-store --dump "$lp" | "${la}sum" | cut -d" " -f1)
fi
[ "$lg" = "$lw" ] || {
    echo "leaf check FAILED: $lp $lm $la got $lg want $lw"; exit 1; }
echo "leaf check OK: $lp $lm $la:$lw"

# 5. Dump the output NAR that CACTEE certifies.
for p in $LEAF_OUTPUTS; do
    nix-store --dump "$p" > "/tmp/outputs/$(basename "$p").nar"
done

# 6. Trace: which hash was verified, which inputs were received.
echo "leaf bundled, verified $lm $la:$lw" > /tmp/inputs-used
ls /tmp/inputs >> /tmp/inputs-used
cp /tmp/inputs-used /tmp/outputs/inputs-used.txt
ls -l /tmp/outputs
"""

RUN_NODE_SH = r"""#!/bin/sh
# run-node.sh -- CACTEE run phase for a NODE.
#
# Network is off. Two things are already in place:
#   - the node's RECIPE, received as the CACTEE input /tmp/inputs/$NODE_EXPORT
#     (a nix-store --export of the .drv closure: this .drv, the input .drvs,
#     the sources). CACTEE hashes it into the certificate, so the certificate
#     says exactly which recipe was executed (Ekin, 2026-09-29);
#   - the input BINARIES, fetched from Harmonia during docker build (the only
#     step with network) and listed in /tmp/input-paths.txt.
# The script imports the recipe, BUILDS the node offline, then dumps what
# CACTEE certifies: the outputs, and the sources the recipe declares (outputs
# reference them at run time, e.g. stdenv embeds move-docs.sh).
#
# Environment (set by the Dockerfile):
#   DRV          /nix/store/<hash>-<name>.drv     the node's recipe
#   NODE_EXPORT  <hash>-<name>.drv.export         the CACTEE input file
set -e

# 0. nixos/nix is multi-user: nix-store needs the daemon, which nothing starts
#    in a plain `docker run`.
if ! [ -S /nix/var/nix/daemon-socket/socket ] 2>/dev/null; then
    nix-daemon --daemon 2>/tmp/nix-daemon.log &
    for _ in $(seq 1 60); do
        [ -S /nix/var/nix/daemon-socket/socket ] 2>/dev/null && break
        sleep 0.1
    done
fi
export NIX_REMOTE=daemon

# 1. Import the recipe received from CACTEE (required).
re="/tmp/inputs/$NODE_EXPORT"
[ -f "$re" ] || { echo "node: $re missing (CACTEE input not provided)"; exit 1; }
nix-store --import < "$re" > /dev/null
test -e "$DRV"

# 2. What the recipe produces, and the sources it declares.
nix-store -q --outputs "$DRV" > /tmp/outpaths
{ nix-store -q --references "$DRV" | grep -v '\.drv$' || true; } > /tmp/srcpaths
echo "outputs: $(wc -l < /tmp/outpaths)  sources: $(wc -l < /tmp/srcpaths)"

# 3. Build it. No substituter: every input must already be in the image, so
#    Nix has to execute the build (the log shows "building '...drv'").
nix-store --realise --option substituters "" "$DRV"

# 4. Dump the outputs and the sources.
sort -u /tmp/outpaths /tmp/srcpaths | while read -r p; do
    nix-store --dump "$p" > "/tmp/outputs/$(basename "$p").nar"
done

# 5. Trace: recipe received, inputs used, sources published.
{
    echo "built offline from recipe $NODE_EXPORT"
    echo "inputs (from Harmonia at build):"
    cat /tmp/input-paths.txt
    echo "sources:"
    cat /tmp/srcpaths
} > /tmp/inputs-used
cp /tmp/inputs-used /tmp/outputs/inputs-used.txt
ls -l /tmp/outputs
"""


def leaf_export_name(output_path: str) -> str:
    """CACTEE input file name for a leaf: the output's store name + .export.

    CACTEE hashes every input file and lists it BY NAME in the certificate,
    so the name says which package went in (Ekin, 2026-09-29), and keeps the
    store hash so it maps back to the exact path.
    """
    return Path(output_path).name + ".export"

# One RUN and one ENV. The classic builder commits the whole fork image
# on every instruction, so each extra ARG/ENV/RUN costs minutes.
DOCKERFILE_HEAD = """FROM {image}

ARG http_proxy
ARG https_proxy
ENV http_proxy=$http_proxy \\
    https_proxy=$https_proxy \\
    HTTP_PROXY=$http_proxy \\
    HTTPS_PROXY=$https_proxy

COPY ca/ /usr/local/share/ca-certificates/
RUN cat /usr/local/share/ca-certificates/zscaler-cert-*.crt >> /etc/ssl/certs/ca-bundle.crt
RUN echo "ssl-cert-file = /etc/ssl/certs/ca-bundle.crt" >> /etc/nix/nix.conf
RUN echo "extra-experimental-features = nix-command" >> /etc/nix/nix.conf
# Binary caches for this build. Not written into the Dockerfile: they are
# deployment details, passed as build arguments from config.json
# ("code_build_args"), which test_docker.py hands to `docker build`.
ARG SUBSTITUTERS
RUN [ -n "$SUBSTITUTERS" ] || (echo "build arg SUBSTITUTERS missing: set it in config.json code_build_args"; exit 1)
RUN echo "substituters = $SUBSTITUTERS" >> /etc/nix/nix.conf
RUN echo "{nix_sig_line}" >> /etc/nix/nix.conf

# --- Contrat CACTEE ---
RUN mkdir -p /tmp/inputs /tmp/outputs

ARG DRV=/nix/store/{drv}
"""

# Leaf (offline): closure export arrives via CACTEE inputs/ → /tmp/inputs at run.
DOCKERFILE_LEAF_OFFLINE = (
    """FROM {image}

ARG http_proxy
ARG https_proxy
ENV http_proxy=$http_proxy \\
    https_proxy=$https_proxy \\
    HTTP_PROXY=$http_proxy \\
    HTTPS_PROXY=$https_proxy

COPY ca/ /usr/local/share/ca-certificates/
RUN cat /usr/local/share/ca-certificates/zscaler-cert-*.crt >> /etc/ssl/certs/ca-bundle.crt \\
 && printf '%s\\n' \\
      'ssl-cert-file = /etc/ssl/certs/ca-bundle.crt' \\
      'extra-experimental-features = nix-command' \\
      'substituters =' \\
      'require-sigs = false' \\
      >> /etc/nix/nix.conf \\
 && mkdir -p /tmp/inputs /tmp/outputs

# CACTEE contract (test_docker.py and property_computation_request.py):
# code/ is the docker build context (network on); inputs/ is copied to
# /tmp/inputs and mounted only at RUN, with network_disabled=True.
# So the leaf closure is NOT part of the image: at run the container imports
# /tmp/inputs/$LEAF_EXPORT, checks the output against the hash pinned
# in the .drv, then dumps it. Nothing leaf-specific happens at build.
ARG DRV=/nix/store/{drv}
ENV DRV=/nix/store/{drv}
ENV LEAF_OUTPUTS="{leaf_outputs}"
ENV LEAF_EXPORT="{leaf_export}"

# Run phase: see code/run-leaf.sh.
COPY run-leaf.sh /usr/local/bin/run-leaf.sh
RUN chmod +x /usr/local/bin/run-leaf.sh
CMD ["sh", "/usr/local/bin/run-leaf.sh"]
"""
)

# Leaf (legacy): fetch during docker build from --drv-source (network on build).
DOCKERFILE_LEAF_ONLINE = DOCKERFILE_HEAD + """
# --- Leaf: binary from cache.nixos.org, not Harmonia ---
RUN nix --extra-experimental-features nix-command copy --from "{drv_source}" {copy_flags} "$DRV"
RUN nix-store --realise --option substituters "{drv_source}" "$DRV"
RUN echo "leaf {drv_source}" > /tmp/inputs-used
RUN nix-store -q --outputs "$DRV" > /tmp/outpaths && cat /tmp/outpaths
CMD ["sh", "-c", "set -e; while read p; do nix-store --dump \\"$p\\" > /tmp/outputs/$(basename \\"$p\\").nar; done < /tmp/outpaths; cp /tmp/inputs-used /tmp/outputs/inputs-used.txt; ls -l /tmp/outputs"]
"""

def leaf_config_inputs(export_name: str) -> list[dict[str, str]]:
    """CACTEE input declaration for a leaf, named after its export file."""
    return [
        {
            "name": export_name[: -len(".export")],
            "upload_method": "direct",
            "input_folder": "inputs/",
        }
    ]

# Non-leaf: Harmonia first for THIS output; miss => fetch .drv and build.
# `|| true`: a custom root (mario) is not on cache.nixos.org; later RUNs
# must handle a missing .drv instead of failing the image (test_docker.py
# would otherwise try to pull test-nix-... from Docker Hub).
DOCKERFILE_NODE = DOCKERFILE_HEAD + """
# CACTEE contract: code/ is the docker build context (network on); inputs/
# is mounted at /tmp/inputs only at RUN, with network_disabled=True.
#   build : fetch the INPUT BINARIES from Harmonia (listed by the generator in
#           input-paths.txt; needs the network)
#   run   : import the RECIPE from the CACTEE input, build offline, dump
#           (code/run-node.sh)
# Whether this node is already on Harmonia is checked by the orchestrator
# before anything is sent to CACTEE.
ENV DRV=/nix/store/{drv}
ENV NODE_EXPORT="{node_export}"

# HARMONIA_URL is a build argument (config.json code_build_args), so the same
# Dockerfile works wherever Harmonia runs.
ARG HARMONIA_URL
RUN [ -n "$HARMONIA_URL" ] || (echo "build arg HARMONIA_URL missing: set it in config.json code_build_args"; exit 1)
COPY input-paths.txt /tmp/input-paths.txt
RUN if [ -s /tmp/input-paths.txt ]; then \\
      nix --extra-experimental-features nix-command copy --from "$HARMONIA_URL" {copy_flags} $(cat /tmp/input-paths.txt); \\
    fi; \\
    echo "inputs from Harmonia: $(wc -l < /tmp/input-paths.txt)"

# Run phase: see code/run-node.sh.
COPY run-node.sh /usr/local/bin/run-node.sh
RUN chmod +x /usr/local/bin/run-node.sh
CMD ["sh", "/usr/local/bin/run-node.sh"]
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


def _make_tree_writable(path: Path) -> None:
    """Best effort: undo root-owned files from Docker/CACTEE on inputs/."""
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        root_p = Path(root)
        for name in files + dirs:
            p = root_p / name
            try:
                mode = p.stat().st_mode
                p.chmod(mode | stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            except OSError:
                pass


def as_store_path(drv: str) -> str:
    if drv.startswith("/nix/store/"):
        return drv
    return f"/nix/store/{drv}"


def nix_run(argv: list[str]) -> str:
    try:
        proc = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        sys.exit(
            "nix-store not found. The generator needs Nix on the host.\n"
            "Install Nix and run it again. (--no-bundle-leaves only writes leaves\n"
            "that the orchestrator refuses.)"
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        sys.exit(f"command failed: {' '.join(argv)}\n{err}")
    return proc.stdout


def store_path_valid(path: str) -> bool:
    try:
        subprocess.run(
            ["nix-store", "-q", path],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def ensure_store_path(path: str, drv_source: str, copy_flags: str) -> None:
    if store_path_valid(path):
        return
    flags = copy_flags.split() if copy_flags.strip() else []
    argv = [
        "nix",
        "--extra-experimental-features",
        "nix-command",
        "copy",
        "--from",
        drv_source,
        *flags,
        path,
    ]
    try:
        subprocess.run(argv, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit(
            "nix not found. The generator needs Nix on the host.\n"
            "Install Nix and run it again. (--no-bundle-leaves only writes leaves\n"
            "that the orchestrator refuses.)"
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        sys.exit(f"cannot fetch {path} from {drv_source}\n{err}")


def leaf_closure_paths(drv: str, drv_source: str, copy_flags: str) -> list[str]:
    """All store paths needed offline in the TEE (drv, outputs, sources, …)."""
    sp = as_store_path(drv)
    ensure_store_path(sp, drv_source, copy_flags)
    try:
        subprocess.run(
            [
                "nix-store",
                "--realise",
                "--option",
                "substituters",
                drv_source,
                sp,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        sys.exit(f"cannot realise {sp} on host for bundling\n{err}")
    # -qR on a .drv does not include the drv path itself (only its output closure).
    paths: set[str] = {sp}
    for line in nix_run(["nix-store", "-qR", sp]).splitlines():
        p = line.strip()
        if p:
            paths.add(p)
    for line in nix_run(["nix-store", "-q", "--outputs", sp]).splitlines():
        out = line.strip()
        if not out:
            continue
        paths.add(out)
        for line in nix_run(["nix-store", "-qR", out]).splitlines():
            p = line.strip()
            if p:
                paths.add(p)
    for p in sorted(paths):
        ensure_store_path(p, drv_source, copy_flags)
    return sorted(paths)


def try_fetch(path: str, drv_source: str, copy_flags: str) -> bool:
    """Like ensure_store_path, but report failure instead of exiting."""
    if store_path_valid(path):
        return True
    flags = copy_flags.split() if copy_flags.strip() else []
    proc = subprocess.run(
        ["nix", "--extra-experimental-features", "nix-command", "copy",
         "--from", drv_source, *flags, path],
        capture_output=True, text=True,
    )
    return proc.returncode == 0 and store_path_valid(path)


def node_export_name(drv: str) -> str:
    """CACTEE input file name for a node: the .drv's store name + .export."""
    return Path(as_store_path(drv)).name + ".export"


def bundle_node_recipe(drv: str, inputs_dir: Path, export_name: str) -> int:
    """Export the node's RECIPE closure into inputs/ as one file.

    `nix-store -qR <drv>` on a derivation gives its recipe closure: this
    .drv, every input .drv, and the sources (scripts, patches) they declare,
    but no build output. The order is kept: dependencies first, which is
    what `nix-store --import` needs.
    """
    sp = as_store_path(drv)
    paths = [ln.strip() for ln in nix_run(["nix-store", "-qR", sp]).splitlines()
             if ln.strip()]
    with (inputs_dir / export_name).open("wb") as fh:
        subprocess.run(["nix-store", "--export", *paths], check=True, stdout=fh)
    return len(paths)


def node_input_paths(drv: str) -> list[str]:
    """Output paths of the node's direct input derivations.

    These are the binaries the build needs; the Dockerfile fetches them from
    Harmonia at build time (nix copy also brings their runtime closure).
    """
    sp = as_store_path(drv)
    refs = [ln.strip() for ln in nix_run(["nix-store", "-q", "--references", sp]).splitlines()
            if ln.strip().endswith(".drv")]
    outs: set[str] = set()
    for d in refs:
        for ln in nix_run(["nix-store", "-q", "--outputs", d]).splitlines():
            if ln.strip():
                outs.add(ln.strip())
    return sorted(outs)


def bundle_leaf_inputs(
    drv: str,
    inputs_dir: Path,
    drv_source: str,
    copy_flags: str,
    export_name: str,
) -> int:
    """Write the leaf closure into inputs/ as ONE nix-store --export file.

    The export carries each path's NAR plus its metadata (path, references,
    deriver), which is what `nix-store --import` needs in the container.
    Per-path .nar files used to be written next to it: they duplicated the
    same bytes (457 MB twice for mario) and nothing read them.
    """
    paths = leaf_closure_paths(drv, drv_source, copy_flags)
    if not paths:
        sys.exit(f"empty closure for {drv}")
    export_path = inputs_dir / export_name
    with export_path.open("wb") as fh:
        subprocess.run(
            ["nix-store", "--export", *paths],
            check=True,
            stdout=fh,
        )
    return len(paths)


def reset_inputs_dir(inputs_dir: Path) -> None:
    """Recreate empty inputs/; old orchestrator copies may be root-owned."""
    if inputs_dir.exists():
        try:
            shutil.rmtree(inputs_dir)
        except PermissionError:
            _make_tree_writable(inputs_dir)
            try:
                shutil.rmtree(inputs_dir)
            except PermissionError as exc:
                pkg = inputs_dir.parent
                sys.exit(
                    f"Cannot clear {inputs_dir}: {exc}\n"
                    "Those files were probably created as root inside Docker.\n"
                    "Fix ownership once, then re-run:\n"
                    f"  sudo chown -R \"$(whoami):\" {pkg.parent}\n"
                    f"  # or only inputs trees:\n"
                    f"  sudo find {pkg.parent} -type d -name inputs "
                    f"-prune -exec chown -R \"$(whoami):\" {{}} +\n"
                )
    inputs_dir.mkdir(parents=True, exist_ok=True)


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
    offline_leaf: bool,
    leaf_outputs: str = "",
    leaf_export: str = "",
    node_export: str = "",
) -> None:
    flags = copy_flags.strip()
    if leaf and offline_leaf:
        tmpl = DOCKERFILE_LEAF_OFFLINE
        text = tmpl.format(
            image=BASE_IMAGE,
            drv=drv,
            leaf_outputs=leaf_outputs,
            leaf_export=leaf_export,
        )
    elif leaf:
        tmpl = DOCKERFILE_LEAF_ONLINE
        text = tmpl.format(
            image=BASE_IMAGE,
            drv=drv,
            substituter=drv_source,
            drv_source=drv_source,
            harmonia=harmonia,
            nix_sig_line=nix_sig_line,
            copy_flags=flags,
        )
    else:
        text = DOCKERFILE_NODE.format(
            image=BASE_IMAGE,
            drv=drv,
            node_export=node_export,
            substituter=substituter,
            drv_source=drv_source,
            harmonia=harmonia,
            nix_sig_line=nix_sig_line,
            copy_flags=flags,
        )
    path.write_text(text, encoding="utf-8", newline="\n")

    # The run-phase script lives next to the Dockerfile in code/, so it is
    # part of the build context test_docker.py tars (and of what CACTEE
    # measures). test_docker.py resets file modes, hence the chmod in the
    # Dockerfile as well.
    code = path.parent
    for stale in ("run-leaf.sh", "run-node.sh"):
        (code / stale).unlink(missing_ok=True)
    if leaf and offline_leaf:
        script, body = code / "run-leaf.sh", RUN_LEAF_SH
    elif not leaf:
        script, body = code / "run-node.sh", RUN_NODE_SH
    else:
        return  # legacy online leaf keeps its inline CMD
    script.write_text(body, encoding="utf-8", newline="\n")
    script.chmod(0o755)


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
    ap.add_argument(
        "--no-bundle-leaves",
        action="store_true",
        help="do not export leaf closures into inputs/ (legacy online leaf Dockerfiles)",
    )
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
    missing: list[tuple[int, str, str]] = []
    n_leaves = 0
    bundle_leaves = not args.no_bundle_leaves

    for idx, drv in enumerate(ordered, 1):
        name = pkg_name(drv)
        folder_name = f"{idx:03d}-{slug(name)}-{hash8(drv)}"
        folder = outdir / folder_name
        code = folder / "code"
        ca_dst = code / "ca"
        if ca_dst.exists():
            shutil.rmtree(ca_dst)
        stale = code / "leaf-inputs"  # older generator: closure copied into code/
        if stale.exists():
            shutil.rmtree(stale)
        code.mkdir(parents=True, exist_ok=True)
        leaf = is_leaf(drv, inputs)
        if leaf:
            n_leaves += 1
        node_subs = drv_source if leaf else substituter
        offline_leaf = leaf and bundle_leaves
        shutil.copytree(ca_dir, ca_dst)

        reset_inputs_dir(folder / "inputs")

        config_inputs: list[dict[str, str]] = []
        leaf_outputs = ""
        leaf_export = ""
        if offline_leaf:
            sp = as_store_path(drv)
            ensure_store_path(sp, drv_source, copy_flags)
            outs = [
                ln.strip()
                for ln in nix_run(["nix-store", "-q", "--outputs", sp]).splitlines()
                if ln.strip()
            ]
            if len(outs) != 1:
                sys.exit(f"leaf {sp} must have exactly one output, got {outs}")
            leaf_outputs = outs[0]
            leaf_export = leaf_export_name(outs[0])
            n_paths = bundle_leaf_inputs(
                drv, folder / "inputs", drv_source, copy_flags, leaf_export
            )
            if n_paths:
                config_inputs = leaf_config_inputs(leaf_export)
            print(
                f"  leaf bundle {idx:03d} {name}: {n_paths} store paths",
                file=sys.stderr,
            )

        node_export = ""
        if not leaf:
            sp = as_store_path(drv)
            if not try_fetch(sp, drv_source, copy_flags):
                # e.g. the root: a local package whose .drv is on no cache.
                print(
                    f"  SKIPPED {idx:03d} {name}: recipe {sp} is neither in the "
                    f"local store nor on {drv_source}. Import it on this host "
                    "(nix-store --import) and regenerate.",
                    file=sys.stderr,
                )
                shutil.rmtree(folder, ignore_errors=True)
                missing.append((idx, drv, name))
                continue
            node_export = node_export_name(drv)
            n_recipe = bundle_node_recipe(drv, folder / "inputs", node_export)
            in_paths = node_input_paths(drv)
            (code / "input-paths.txt").write_text(
                "".join(p + "\n" for p in in_paths), encoding="utf-8", newline="\n"
            )
            config_inputs = [{
                "name": node_export[: -len(".export")],
                "upload_method": "direct",
                "input_folder": "inputs/",
            }]
            print(
                f"  node recipe {idx:03d} {name}: {n_recipe} recipe paths, "
                f"{len(in_paths)} input binaries",
                file=sys.stderr,
            )
        write_dockerfile(
            code / "Dockerfile",
            drv,
            node_subs,
            nix_sig_line,
            copy_flags,
            drv_source,
            harmonia,
            leaf,
            offline_leaf,
            leaf_outputs=leaf_outputs,
            leaf_export=leaf_export,
            node_export=node_export,
        )
        # Nodes: input = the recipe; input binaries come from Harmonia at build.

        config = {
            "name": f"nix/{slug(name)}-{hash8(drv)}",
            "inputs": config_inputs,
            "code_folder": "code/",
        }
        # Deployment-specific addresses go to docker build as build
        # arguments (test_docker.py: config["code_build_args"] -> buildargs),
        # not into the Dockerfile text. Change them here, no regeneration.
        if not leaf:
            config["code_build_args"] = {
                "HARMONIA_URL": harmonia,
                "SUBSTITUTERS": node_subs,
            }
        elif not offline_leaf:
            config["code_build_args"] = {"SUBSTITUTERS": node_subs}
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
        for idx, drv, name in missing:
            fh.write(f"# {idx:03d}  SKIPPED  recipe not available  /nix/store/{drv}\n")

    print(f"HTML instances            : {len(inst)}")
    print(f"packages generated        : {len(generated)}")
    if missing:
        print(f"SKIPPED (no recipe)       : {len(missing)} -> "
              + ", ".join(f"{i:03d} {n}" for i, _, n in missing))
    if bundle_leaves:
        print(f"leaves (bundled offline)  : {n_leaves}")
    else:
        print(f"leaves (online fetch)     : {n_leaves}")
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