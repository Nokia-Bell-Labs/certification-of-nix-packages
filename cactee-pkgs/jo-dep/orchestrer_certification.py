#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""
Certify generated CACTEE packages in two phases, then push each result to
Harmonia.

  1. all leaves (no .drv input: bootstrap tarball, busybox, source fetches)
  2. all remaining nodes, still bottom-up (the rest of 00-build-order.txt)

Leaves first, because nodes are supposed to take certified binaries from
Harmonia. Mixing them in one pass would try to certify gcc before busybox
is on the cache.

For each package:

  1. resolve direct .drv inputs and their output store paths
  2. for each dependency already certified, drop its .nar and its
     <nar>_asset_certificate.json into inputs/ and declare them in config.json
  3. run the certification (service) or the local harness (local)
  4. collect the .nar files and the asset certificate
  5. import the .nar, push it to Harmonia, store the certificate
  6. record the package as done so the run can resume

CACTEE treats a file named <nar>_asset_certificate.json as the certificate of
that input, not as a plain file. That is the chain.

Modes
    local    test_docker.py — .nar, no TEE, no certificate
    service  test_asset_certification.py — needs DUET_ADMIN_URL

State
    orchestration-state.json. Re-running skips packages already done.

Usage
    python3 orchestrer_certification.py --dry-run
    python3 orchestrer_certification.py --mode local --phase leaves
    python3 orchestrer_certification.py --mode local --phase nodes
    python3 orchestrer_certification.py --mode service --phase all --from 1 --to 40
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# 001  L48  leaf  /nix/store/….drv   (current generator)
# 001  L48  /nix/store/….drv         (older)
# 001  /nix/store/….drv
ORDER_LINE = re.compile(
    r"^(\d+)\s+(?:(L\S+)\s+)?(?:(leaf|node)\s+)?(/nix/store/\S+\.drv)\s*$"
)
ARG_DRV = re.compile(r'^ARG\s+DRV=(/nix/store/\S+\.drv)', re.M)
EDGE = re.compile(r'^"([^"]+)"\s*->\s*"([^"]+)"')


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


class Nix:
    """Nix queries, run through a configurable prefix.

    The store usually lives inside a container, hence the prefix.
    """

    def __init__(self, prefix: str):
        self.prefix = prefix.strip()

    def _q(self, args: str) -> str:
        cmd = f"{self.prefix} sh -c {json.dumps(args)}" if self.prefix else args
        _, out, _ = run(cmd)
        return out

    def check(self) -> str:
        """Fail loudly if nix is not reachable, instead of reporting 0 inputs."""
        try:
            _, out, _ = run(
                f"{self.prefix} sh -c 'nix-store --version'" if self.prefix
                else "nix-store --version")
            return out.strip()
        except RuntimeError as exc:
            raise SystemExit(
                "nix-store is not reachable"
                + (f" through '{self.prefix}'" if self.prefix else " locally")
                + ".\nSet --nix-prefix to whatever gives access to the store, "
                  "for example:\n"
                  "  --nix-prefix 'docker exec nixtee-publisher'\n"
                  "  --nix-prefix 'ssh -p 2225 ub-4@localhost'\n"
                  f"\nunderlying error: {exc}"
            ) from exc

    def drv_inputs(self, drv: str) -> list[str]:
        # Do not pipe to grep: a missing .drv makes nix-store fail, grep
        # gets empty stdin, and `test $? -le 1` would report 0 inputs.
        out = self._q(f'nix-store -q --references "{drv}"')
        return [l.strip() for l in out.splitlines()
                if l.strip().endswith(".drv")]

    def drv_outputs(self, drv: str) -> list[str]:
        out = self._q(f'nix-store -q --outputs "{drv}"')
        return [l.strip() for l in out.splitlines() if l.strip()]


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"done": {}, "failed": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        self.data.setdefault("done", {})
        self.data.setdefault("failed", {})

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def is_done(self, drv: str) -> bool:
        return drv in self.data["done"]

    def mark_done(self, drv: str, info: dict):
        self.data["done"][drv] = info
        self.data["failed"].pop(drv, None)
        self.save()

    def mark_failed(self, drv: str, reason: str):
        self.data["failed"][drv] = {"reason": reason[-1500:], "when": time.time()}
        self.save()

    def certified_outputs(self) -> dict[str, dict]:
        out = {}
        for info in self.data["done"].values():
            for path, files in info.get("outputs", {}).items():
                out[path] = files
        return out


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


def find_graph(explicit: Path | None) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            sys.exit(f"graph not found: {explicit}")
        return explicit
    matches = sorted(Path(".").glob("*-drv-graph.dot"))
    return matches[0] if matches else None


def parse_drv_graph(path: Path) -> dict[str, list[str]]:
    """dst .drv -> unique src .drv inputs. Store paths, .drv edges only."""
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
    return dict(deps)


def find_package_dir(pkgs_root: Path, idx: int, drv: str) -> Path | None:
    prefix = f"{idx:03d}-"
    for child in sorted(pkgs_root.iterdir()):
        if child.is_dir() and child.name.startswith(prefix):
            return child
    for child in sorted(pkgs_root.iterdir()):
        dockerfile = child / "code" / "Dockerfile"
        if dockerfile.is_file():
            m = ARG_DRV.search(dockerfile.read_text(encoding="utf-8"))
            if m and m.group(1) == drv:
                return child
    return None


def prepare_inputs(pkg_dir: Path, dep_drvs: list[str], state: State,
                   verbose=True) -> tuple[int, int]:
    """Populate inputs/ from already-certified .drv dependencies."""
    inputs_dir = pkg_dir / "inputs"
    shutil.rmtree(inputs_dir, ignore_errors=True)

    chained, missing = 0, 0
    for dep_drv in dep_drvs:
        info = state.data["done"].get(dep_drv)
        outputs = (info or {}).get("outputs") or {}
        if not outputs:
            missing += 1
            continue
        for dep_out, files in outputs.items():
            inputs_dir.mkdir(parents=True, exist_ok=True)
            nar_name = f"{Path(dep_out).name}.nar"
            shutil.copy2(files["nar"], inputs_dir / nar_name)
            cert_src = files.get("certificate")
            if cert_src:
                shutil.copy2(cert_src,
                             inputs_dir / f"{nar_name}_asset_certificate.json")
            chained += 1
            if verbose:
                print(f"      chained {nar_name}")

    cfg_path = pkg_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if chained:
        cfg["inputs"] = [{"name": "deps",
                          "upload_method": "direct",
                          "input_folder": "inputs/"}]
    else:
        cfg["inputs"] = []
    cfg_path.write_text(json.dumps(cfg, indent=4) + "\n", encoding="utf-8")

    return chained, missing


def certify_local(cactee: Path, pkg_dir: Path) -> Path:
    harness = cactee / "examples" / "dev_test_docker"
    cfg = (pkg_dir / "config.json").resolve()
    rc, out, err = run([sys.executable, "test_docker.py", str(cfg)],
                       check=False, cwd=harness)
    log = out + err
    if rc != 0:
        raise RuntimeError(f"test_docker.py failed\n{log[-2000:]}")
    if "building '" not in log and "copying path" in log:
        raise RuntimeError(
            "no 'building' line in the log: the target was substituted, "
            "not built. Certifying this would attest a copy."
        )
    outputs = harness / "test_temp" / "tmp" / "outputs"
    if not any(outputs.glob("*.nar")):
        raise RuntimeError(f"no .nar produced in {outputs}")
    return outputs


def certify_service(cactee: Path, pkg_dir: Path, name: str) -> Path:
    cfg = (pkg_dir / "config.json").resolve()
    rc, out, err = run([sys.executable, "test_asset_certification.py", str(cfg)],
                       check=False, cwd=cactee)
    if rc != 0:
        raise RuntimeError(f"test_asset_certification.py failed\n{(out+err)[-2000:]}")
    results = cactee / "test_results" / "certifications" / name
    if not (results / "asset_certificate.json").is_file():
        raise RuntimeError(f"no asset_certificate.json in {results}")
    return results


def publish(nar: Path, cert: Path | None, store_path: str,
            nix: Nix, harmonia_to: str | None, cert_dir: Path | None,
            dry_run=False) -> None:
    if dry_run:
        print(f"      would publish {Path(store_path).name}")
        return

    tmp = f"/tmp/restore-{hash_part(store_path)}"
    prefix = nix.prefix
    if prefix:
        run(f'{prefix} sh -c "rm -rf {tmp}"', check=False)
        run(f'{prefix} sh -c "cat > {tmp}.nar" < {nar}')
        run(f'{prefix} sh -c "nix-store --restore {tmp} < {tmp}.nar"')
    else:
        run(f'nix-store --restore {tmp} < "{nar}"')

    if harmonia_to:
        cmd = f'nix copy --no-check-sigs --to "{harmonia_to}" "{store_path}"'
        run(f"{prefix} sh -c {json.dumps(cmd)}" if prefix else cmd)

    if cert and cert_dir:
        cert_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cert, cert_dir / f"{hash_part(store_path)}.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-order", type=Path,
                    default=Path("generated/00-build-order.txt"))
    ap.add_argument("--pkgs-root", type=Path, default=Path("generated"))
    ap.add_argument("--cactee", type=Path,
                    default=Path.home() / "nix-tee" /
                    "confidential-asset-certification-using-tees",
                    help="the CACTEE checkout")
    ap.add_argument("--mode", choices=["local", "service"], default="local")
    ap.add_argument("--nix-prefix", default="docker exec nixtee-publisher",
                    help="prefix for nix commands; empty to run them locally")
    ap.add_argument("--harmonia-to",
                    help="nix copy --to target, e.g. ssh://ub-4?store=/nix "
                         "or file:///srv/harmonia-store")
    ap.add_argument("--cert-dir", type=Path,
                    help="folder Harmonia reads certificates from; "
                         "written as <hashPart>.json")
    ap.add_argument("--state", type=Path, default=Path("orchestration-state.json"))
    ap.add_argument("--from", dest="start", type=int, default=1)
    ap.add_argument("--to", dest="end", type=int)
    ap.add_argument(
        "--phase",
        choices=["leaves", "nodes", "all"],
        default="all",
        help="leaves first (no .drv input), then remaining nodes bottom-up",
    )
    ap.add_argument("--leaves-only", action="store_true",
                    help="same as --phase leaves")
    ap.add_argument("--stop-on-error", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--graph", type=Path,
                    help="nix-store --graph .dot (default: ./*-drv-graph.dot)")
    args = ap.parse_args()

    if not args.build_order.is_file():
        sys.exit(f"build order not found: {args.build_order}")
    if not args.pkgs_root.is_dir():
        sys.exit(f"packages root not found: {args.pkgs_root}")

    graph_path = find_graph(args.graph)
    graph = parse_drv_graph(graph_path) if graph_path else None

    nix = None
    nix_version = "unused (dry-run uses the .dot graph)"
    if not args.dry_run:
        nix = Nix(args.nix_prefix)
        nix_version = nix.check()

    state = State(args.state)
    order = read_order(args.build_order)
    if not order:
        sys.exit(f"no package lines parsed from {args.build_order}")

    phase = "leaves" if args.leaves_only else args.phase
    print(f"packages in order : {len(order)}")
    print(f"already done      : {len(state.data['done'])}")
    print(f"mode              : {args.mode}")
    print(f"phase             : {phase}  (leaves, then nodes bottom-up)")
    print(f"graph             : {graph_path or '(none)'}")
    print(f"nix prefix        : {args.nix_prefix or '(local)'}  [{nix_version}]")
    print(f"harmonia target   : {args.harmonia_to or '(none)'}")
    print()

    classified: list[tuple[int, str, bool, Path, str, list[str]]] = []
    for idx, drv, kind in order:
        if idx < args.start or (args.end and idx > args.end):
            continue
        pkg_dir = find_package_dir(args.pkgs_root, idx, drv)
        if pkg_dir is None:
            print(f"[{idx:03d}] no package folder, skipped")
            continue
        cfg = json.loads((pkg_dir / "config.json").read_text(encoding="utf-8"))
        name = cfg["name"]
        if graph is not None:
            deps = graph.get(drv, [])
        elif nix is not None:
            try:
                deps = nix.drv_inputs(drv)
            except RuntimeError as exc:
                print(f"[{idx:03d}] {name}: cannot query inputs -- {exc}")
                state.mark_failed(drv, str(exc))
                if args.stop_on_error:
                    sys.exit(1)
                continue
        else:
            deps = []
        is_leaf = kind == "leaf" if kind else not deps
        classified.append((idx, drv, is_leaf, pkg_dir, name, deps))

    leaves = [p for p in classified if p[2]]
    nodes = [p for p in classified if not p[2]]
    if phase == "leaves":
        queue = [("leaves", leaves)]
    elif phase == "nodes":
        queue = [("nodes (bottom-up)", nodes)]
    else:
        queue = [("leaves", leaves), ("nodes (bottom-up)", nodes)]

    done = skipped = failed = 0
    stop = False
    for label, batch in queue:
        if stop:
            break
        print(f"===== phase: {label} ({len(batch)} packages) =====")
        for idx, drv, is_leaf, pkg_dir, name, deps in batch:
            if state.is_done(drv):
                skipped += 1
                continue

            print(f"[{idx:03d}] {'leaf' if is_leaf else 'node'}  {name}")
            print(f"      drv  {drv}")
            print(f"      direct .drv inputs: {len(deps)}")

            try:
                if args.dry_run:
                    print("      dry run, nothing executed")
                    skipped += 1
                    continue

                chained, missing = prepare_inputs(pkg_dir, deps, state)
                if chained or missing:
                    print(f"      certificates chained: {chained}, "
                          f"not yet certified: {missing}")

                if args.mode == "local":
                    out_dir = certify_local(args.cactee, pkg_dir)
                    cert = None
                else:
                    out_dir = certify_service(args.cactee, pkg_dir, name)
                    cert = out_dir / "asset_certificate.json"

                produced = {}
                for nar in sorted(out_dir.glob("*.nar")):
                    store_path = "/nix/store/" + nar.name[:-4]
                    keep = pkg_dir / "results"
                    keep.mkdir(exist_ok=True)
                    kept_nar = keep / nar.name
                    shutil.copy2(nar, kept_nar)
                    kept_cert = None
                    if cert:
                        kept_cert = keep / f"{nar.name}_asset_certificate.json"
                        shutil.copy2(cert, kept_cert)
                    publish(kept_nar, kept_cert, store_path, nix,
                            args.harmonia_to, args.cert_dir)
                    produced[store_path] = {
                        "nar": str(kept_nar),
                        "certificate": str(kept_cert) if kept_cert else None,
                    }
                    print(f"      published {nar.name}")

                state.mark_done(drv, {"idx": idx, "name": name,
                                      "outputs": produced, "when": time.time()})
                done += 1

            except Exception as exc:                      # noqa: BLE001
                print(f"      FAILED: {exc}")
                state.mark_failed(drv, str(exc))
                failed += 1
                if args.stop_on_error:
                    stop = True
                    break

    print()
    print(f"done {done}   skipped {skipped}   failed {failed}")
    if not args.dry_run and state.data["failed"]:
        print("\nfailures:")
        for drv, info in state.data["failed"].items():
            print(f"  {Path(drv).name}: {info['reason'].splitlines()[0][:120]}")
    if args.dry_run:
        print("\nno state file written (dry-run)")
    elif args.state.exists():
        print(f"\nstate in {args.state}")
    else:
        print("\nno state file yet (nothing done or failed)")


if __name__ == "__main__":
    main()
