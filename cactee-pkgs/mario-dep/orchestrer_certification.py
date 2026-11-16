#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""
Run bottom-up CACTEE certification and publish NARs to Harmonia.

Packages are produced by generer_dockerfiles.py (config.json, code/, inputs/).
This script does not modify config.json or populate inputs/.

Phases (see --phase):

  1. leaves — no .drv inputs (bootstrap tarball, busybox, fixed-output fetches)
  2. nodes — their only CACTEE input is their recipe (.drv closure export);
     their input binaries come from Harmonia during docker build

Leaves run first so nodes can substitute certified outputs from Harmonia.
Cert-aware verification of attestation JSON on fetch is planned in Nix, not
in this script.

For each package:

  1. skip if every output is already on Harmonia: GET /<hash>.narinfo for
     each expected output path (exact hash only, never by name)
  2. POST the package dir unchanged (no edits to config.json or inputs/)
  3. copy .nar + certificate into results/ (local audit)
  4. PUT each certificate to the cache import API (:5001/attest/<hash>.json),
     then its .nar (:5001/import)

The cache import API verifies the certificate first (quote, signature, service),
then checks the NAR against it, and only then registers the NAR under its
ORIGINAL store path, computing its references by scanning the NAR (see
nar-import-server.py). It answers 412 when no verified certificate was sent,
409 when the NAR differs from the one the certificate names or when it
mentions a store path that is not certified yet, 422 for a certificate that
does not verify. For a derivation with several outputs that reference each
other (out -> lib), the outputs are retried until no more progress is made, so
the order in which CACTEE returns them does not matter.

There is no orchestrator-side NAR or certificate chaining into inputs/, and
nothing is imported into the local Nix store.

Certification is always HTTP to --cactee-url / CACTEE_URL (nixtee-cactee).
The container runs the harness and returns a simulated certificate (not a
real TEE quote).

Usage
    python3 orchestrer_certification.py --cactee-url http://cactee:8080
    python3 orchestrer_certification.py --phase leaves
    python3 orchestrer_certification.py --phase nodes
    python3 orchestrer_certification.py --harmonia-to http://127.0.0.1:5000
    python3 publish_nar.py --to http://127.0.0.1:5000 --store-path /nix/store/… file.nar [--cert cert.json]

HTTP --harmonia-to is the Harmonia read URL (port 5000). NARs are uploaded to
port 5001 /import with HARMONIA_PUBLISH_TOKEN (HTTP only; no SSH publish).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

from publish_nar import publish_nar

# 001  L48  leaf  /nix/store/….drv   (current generator)
# 001  L48  /nix/store/….drv         (older)
# 001  /nix/store/….drv
ORDER_LINE = re.compile(
    r"^(\d+)\s+(?:(L\S+)\s+)?(?:(leaf|node)\s+)?(/nix/store/\S+\.drv)\s*$"
)
ARG_DRV = re.compile(r'^ARG\s+DRV=(/nix/store/\S+\.drv)', re.M)
ENV_LEAF_OUTPUTS = re.compile(r'^ENV LEAF_OUTPUTS="([^"]+)"', re.M)
EDGE = re.compile(r'^"([^"]+)"\s*->\s*"([^"]+)"')
STORE_PATH_RE = re.compile(r"^/nix/store/[0-9a-z]{32}-[A-Za-z0-9._+?=-]+$")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def run(cmd, check=True, capture=True, cwd=None, env=None):
    """Run a command, return (rc, stdout, stderr)."""
    proc = subprocess.run(
        cmd, shell=isinstance(cmd, str), cwd=cwd, env=env,
        capture_output=capture, text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {cmd}\n"
            f"{(proc.stderr or '')[-2000:]}"
        )
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def hash_part(store_path: str) -> str:
    """/nix/store/abc123...-name -> abc123..."""
    return Path(store_path).name.split("-", 1)[0]


def check_package_role(is_leaf: bool, cfg: dict, name: str) -> None:
    """Every package declares exactly one CACTEE input, written by the generator.

    Leaves: their closure export. Nodes: their recipe export (.drv closure),
    so the certificate records which recipe was executed. Input binaries of
    a node come from Harmonia during docker build, never through inputs/.
    """
    inputs = cfg.get("inputs") or []
    if len(inputs) != 1:
        raise ValueError(
            f"{name}: expected exactly one CACTEE input "
            f"({'closure' if is_leaf else 'recipe'} export), found {len(inputs)}. "
            "Regenerate with generer_dockerfiles.py."
        )


def harmonia_has(harmonia_to: str, store_path: str) -> bool:
    """True if GET /<32hex>.narinfo returns 200 AND names this exact path."""
    hid = hash_part(store_path)
    if len(hid) != 32:
        return False
    url = f"{harmonia_to.rstrip('/')}/{hid}.narinfo"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            if not 200 <= resp.status < 300:
                return False
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False
    for line in body.splitlines():
        if line.startswith("StorePath:"):
            return line.split(":", 1)[1].strip() == store_path
    return False


def drv_output_paths(drv: str) -> list[str]:
    """Outputs of a .drv from the host Nix store (empty if drv is not local)."""
    sp = as_store(drv)
    try:
        _, out, _ = run(f'nix-store -q --outputs "{sp}"', check=True)
    except RuntimeError:
        return []
    return [
        ln.strip()
        for ln in out.splitlines()
        if ln.strip().startswith("/nix/store/")
    ]


def expected_outputs(pkg_dir: Path, drv: str) -> list[str]:
    """Store paths this package produces — for the Harmonia skip check.

    Sources, in order: the .drv (if it is in the local store), LEAF_OUTPUTS in
    the leaf Dockerfile, then the .nar names kept in results/ by a previous
    run. All of them are original store paths, so an exact-hash lookup on
    Harmonia is enough.
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(path: str) -> None:
        if STORE_PATH_RE.match(path) and path not in seen:
            seen.add(path)
            out.append(path)

    for p in drv_output_paths(drv):
        add(p)

    dockerfile = pkg_dir / "code" / "Dockerfile"
    if dockerfile.is_file():
        text = dockerfile.read_text(encoding="utf-8")
        m = ENV_LEAF_OUTPUTS.search(text)
        if m:
            for p in m.group(1).split():
                add(p)

    if out:
        return out

    results = pkg_dir / "results"
    if results.is_dir():
        for nar in sorted(results.glob("*.nar")):
            add("/nix/store/" + nar.name[:-4])
    return out


def on_harmonia(harmonia_to: str | None, pkg_dir: Path, drv: str) -> bool:
    """True if Harmonia already has every output of this package (exact hash)."""
    if not harmonia_to or not harmonia_to.startswith(("http://", "https://")):
        return False
    candidates = expected_outputs(pkg_dir, drv)
    if not candidates:
        return False
    return all(harmonia_has(harmonia_to, p) for p in candidates)


def drv_inputs(drv: str) -> list[str]:
    # Do not pipe to grep: a missing .drv makes nix-store fail, grep
    # gets empty stdin, and `test $? -le 1` would report 0 inputs.
    _, out, _ = run(f'nix-store -q --references "{drv}"')
    return [l.strip() for l in out.splitlines()
            if l.strip().endswith(".drv")]


# --------------------------------------------------------------------------
# build order
# --------------------------------------------------------------------------

def read_order(path: Path) -> list[tuple[int, str, str | None]]:
    """Return (idx, drv, kind) where kind is 'leaf', 'node', or None."""
    items = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = ORDER_LINE.match(line)
        if m:
            idx, _layer, kind, drv = m.groups()
            items.append((int(idx), drv, kind))
    return items


def as_store(drv: str) -> str:
    return drv if drv.startswith("/nix/store/") else f"/nix/store/{drv}"


def load_drv_graph() -> tuple[Path | None, dict[str, list[str]]]:
    """Read .drv edges from ./*-drv-graph.dot (no CLI flag)."""
    matches = sorted(Path(".").glob("*-drv-graph.dot"))
    if not matches:
        return None, {}
    path = matches[0]
    deps: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = EDGE.match(raw.strip())
        if not m:
            continue
        src, dst = m.group(1), m.group(2)
        if not src.endswith(".drv") or not dst.endswith(".drv"):
            continue
        src, dst = as_store(src), as_store(dst)
        if src not in seen[dst]:
            seen[dst].add(src)
            deps[dst].append(src)
    return path, dict(deps)


def find_package_dir(pkgs_root: Path, idx: int, drv: str) -> Path | None:
    """The generator names folders NNN-<name>-<hash8>."""
    prefix = f"{idx:03d}-"
    for child in sorted(pkgs_root.iterdir()):
        if child.is_dir() and child.name.startswith(prefix):
            return child
    # fall back on the ARG DRV inside each Dockerfile
    for child in sorted(pkgs_root.iterdir()):
        dockerfile = child / "code" / "Dockerfile"
        if dockerfile.is_file():
            m = ARG_DRV.search(dockerfile.read_text(encoding="utf-8"))
            if m and m.group(1) == drv:
                return child
    return None


# --------------------------------------------------------------------------
# certification
# --------------------------------------------------------------------------

SKIP_PKG_PARTS = {"results", "__pycache__"}


def extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
    tar.extractall(dest, **kwargs)


def pack_pkg_dir(pkg_dir: Path) -> bytes:
    """Gzip tar of config.json, code/, inputs/. No results/."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(pkg_dir.rglob("*")):
            rel = path.relative_to(pkg_dir)
            if rel.parts and rel.parts[0] in SKIP_PKG_PARTS:
                continue
            tar.add(path, arcname=str(rel), recursive=False)
    return buf.getvalue()


def certify_http(url: str, pkg_dir: Path, require_build: bool = False) -> Path:
    """POST the package dir to the CACTEE container. Returns a folder of .nar."""
    endpoint = url.rstrip("/") + "/certify"
    data = pack_pkg_dir(pkg_dir)
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/gzip",
            "Content-Length": str(len(data)),
            "X-Require-Build": "1" if require_build else "0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=7200) as resp:
            raw = resp.read()
            http_ok = 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        http_ok = False
        if not raw:
            raise RuntimeError(f"{endpoint} HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"{endpoint} failed: {exc.reason}. Is nixtee-cactee up?"
        ) from exc

    out_dir = Path(tempfile.mkdtemp(prefix="cactee-out-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            extract_tar(tar, out_dir)
    except tarfile.TarError as exc:
        text = raw.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"{endpoint} not a result tar: {exc}\n{text}") from exc

    result_path = out_dir / "result.json"
    result = {}
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
    log = result.get("log_tail") or ""
    rc = result.get("rc", 0 if http_ok else 1)
    if rc != 0:
        raise RuntimeError(f"cactee certify failed (rc={rc})\n{log[-2500:]}")
    if require_build:
        built = (
            "building '" in log
            or "built locally" in log
            or "built with certified" in log
        )
        if not built and "copying path" in log:
            raise RuntimeError(
                "no 'building' line in the log: the target was substituted, "
                "not built. Certifying this would attest a copy."
            )
    if not any(out_dir.glob("*.nar")):
        raise RuntimeError(f"no .nar returned by {endpoint}\n{log[-1500:]}")
    return out_dir


# --------------------------------------------------------------------------
# publish to Harmonia
# --------------------------------------------------------------------------

def resolve_harmonia_push(url: str | None) -> str | None:
    """Keep HTTP(S) as-is (NAR PUT to :5001). Strip trailing slashes."""
    if not url:
        return None
    return url.strip().rstrip("/")


def is_missing_dependency(exc: Exception) -> bool:
    """409 from the import API because a referenced path is not certified yet."""
    text = str(exc)
    return "HTTP 409" in text and "not in the certified store" in text


def publish_outputs(
    items: list[tuple[Path, Path | None, str]],
    harmonia_to: str,
) -> None:
    """PUT every (nar, cert, store_path) of ONE package to the cache.

    Outputs of the same derivation may reference each other (out -> lib).
    A 409 "not in the certified store" is retried after the other outputs
    went in; the loop stops when a full pass makes no progress.
    """
    if not harmonia_to.startswith(("http://", "https://")):
        raise ValueError(
            f"--harmonia-to must be http(s) (got {harmonia_to!r}); "
            "SSH / nix copy publish is not supported"
        )
    pending = list(items)
    last_errors: dict[str, str] = {}
    while pending:
        retry: list[tuple[Path, Path | None, str]] = []
        for nar, cert, store_path in pending:
            try:
                publish_nar(harmonia_to, nar, store_path, cert)
                print(f"      published {Path(store_path).name}", flush=True)
                last_errors.pop(store_path, None)
            except RuntimeError as exc:
                if not is_missing_dependency(exc):
                    raise
                last_errors[store_path] = str(exc)
                retry.append((nar, cert, store_path))
        if len(retry) == len(pending):
            # No progress: the missing paths are not outputs of this package.
            missing: set[str] = set()
            for text in last_errors.values():
                tail = text.rsplit("):", 1)[-1]
                missing.update(re.findall(r"\b[0-9a-z]{32}\b", tail))
            raise RuntimeError(
                f"{len(retry)} output(s) refused, uncertified dependencies: "
                + " ".join(sorted(missing))
            )
        pending = retry


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-order", type=Path,
                    default=Path("generated/00-build-order.txt"))
    ap.add_argument("--pkgs-root", type=Path, default=Path("generated"))
    ap.add_argument("--cactee-url",
                    default=os.environ.get("CACTEE_URL", ""),
                    help="The url of CACTEE service,ex: http://127.0.0.1:8080"
                    " or http://cactee-service.example.com:8080 ")
    ap.add_argument("--harmonia-to",
                    default=os.environ.get("HARMONIA_URL", ""),
                    help="Harmonia read URL,ex: http://127.0.0.1:5000 or "
                         "http://harmonia.example.com:5000 ")
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--to", dest="end", type=int)
    ap.add_argument(
        "--phase",
        choices=["leaves", "nodes", "all"],
        default="all",
        help="leaves first (no .drv input), then remaining nodes bottom-up",
    )
    ap.add_argument("--stop-on-error", action="store_true")
    args = ap.parse_args()
    args.harmonia_to = resolve_harmonia_push(args.harmonia_to)

    if not args.build_order.is_file():
        sys.exit(f"build order not found: {args.build_order}")
    if not args.pkgs_root.is_dir():
        sys.exit(f"packages root not found: {args.pkgs_root}")
    if not args.cactee_url:
        sys.exit("need --cactee-url ex: http://cactee-service.example.com:8080 ")

    order = read_order(args.build_order)
    if not order:
        sys.exit(f"no package lines parsed from {args.build_order}")

    _graph_path, graph = load_drv_graph()

    drv_pkg: dict[str, Path] = {}
    for idx, drv, _kind in order:
        pkg_dir = find_package_dir(args.pkgs_root, idx, drv)
        if pkg_dir is not None:
            drv_pkg[drv] = pkg_dir

    print(f"packages in order : {len(order)}")
    print(f"cactee            : {args.cactee_url}")
    print(f"phase             : {args.phase} ")
    print(f"harmonia target   : {args.harmonia_to or '(none)'}")
    print()

    classified: list[tuple[int, str, bool, Path, str, list[str]]] = []
    failures: list[tuple[str, str]] = []
    for idx, drv, kind in order:
        if idx < args.start or (args.end and idx > args.end):
            continue
        pkg_dir = drv_pkg.get(drv)
        if pkg_dir is None:
            print(f"[{idx:03d}] no package folder, skipped")
            continue
        cfg = json.loads((pkg_dir / "config.json").read_text(encoding="utf-8"))
        name = cfg["name"]
        if kind == "leaf":
            deps = []
        elif graph:
            deps = graph.get(drv, [])
        else:
            try:
                deps = drv_inputs(drv)
            except RuntimeError as exc:
                print(f"[{idx:03d}] {name}: cannot query inputs -- {exc}")
                failures.append((name, str(exc)))
                if args.stop_on_error:
                    sys.exit(1)
                continue
        is_leaf = kind == "leaf" if kind else not deps
        classified.append((idx, drv, is_leaf, pkg_dir, name, deps))

    leaves = [p for p in classified if p[2]]
    nodes = [p for p in classified if not p[2]]
    if args.phase == "leaves":
        queue = [("leaves", leaves)]
    elif args.phase == "nodes":
        queue = [("nodes", nodes)]
    else:
        queue = [("leaves", leaves), ("nodes", nodes)]

    done = skipped = failed = 0
    stop = False
    for label, batch in queue:
        if stop:
            break
        print(f"===== phase: {label} ({len(batch)} packages) =====")
        for idx, drv, is_leaf, pkg_dir, name, deps in batch:
            if on_harmonia(args.harmonia_to, pkg_dir, drv):
                print(f"[{idx:03d}] skip  {name}  (already on harmonia)")
                skipped += 1
                continue

            print(f"[{idx:03d}] {'leaf' if is_leaf else 'node'}  {name}", flush=True)
            print(f"      drv  {drv}", flush=True)
            print(f"      direct .drv inputs: {len(deps)}", flush=True)

            try:
                cfg = json.loads((pkg_dir / "config.json").read_text(encoding="utf-8"))
                check_package_role(is_leaf, cfg, name)

                print(f"      POST {args.cactee_url}/certify "
                      f"(CACTEE docker build, can take several minutes)",
                      flush=True)
                out_dir = certify_http(
                    args.cactee_url, pkg_dir, require_build=not is_leaf)
                print("      CACTEE returned", flush=True)

                keep = pkg_dir / "results"
                keep.mkdir(exist_ok=True)
                items: list[tuple[Path, Path | None, str]] = []
                for nar in sorted(out_dir.glob("*.nar")):
                    store_path = "/nix/store/" + nar.name[:-4]
                    kept_nar = keep / nar.name
                    shutil.copy2(nar, kept_nar)
                    cert = out_dir / f"{nar.name}_asset_certificate.json"
                    if not cert.is_file():
                        cert = out_dir / "asset_certificate.json"
                    kept_cert = None
                    if cert.is_file():
                        kept_cert = keep / f"{nar.name}_asset_certificate.json"
                        shutil.copy2(cert, kept_cert)
                    items.append((kept_nar, kept_cert, store_path))
                shutil.rmtree(out_dir, ignore_errors=True)

                if args.harmonia_to:
                    publish_outputs(items, args.harmonia_to)
                else:
                    print("      no --harmonia-to: kept in results/ only")

                done += 1

            except Exception as exc:                      # noqa: BLE001
                print(f"      FAILED: {exc}")
                failures.append((name, str(exc)))
                failed += 1
                if args.stop_on_error:
                    stop = True
                    break

    print()
    print(f"done {done}   skipped {skipped}   failed {failed}")
    if failures:
        print("\nfailures:")
        for name, reason in failures:
            print(f"  {name}: {reason.splitlines()[0][:120]}")


if __name__ == "__main__":
    main()