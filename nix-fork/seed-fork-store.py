#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Copy the /nix/store items the fork binary was linked against to /opt.

The publisher volume hides /nix. node-init copies these items back into
/nix/store on startup. The binary's interpreter path stays unchanged.
"""
import glob
import os
import shutil
import struct
from pathlib import Path

MIRROR = Path("/opt/fork-closure/nix-store")
PREFIX = Path("/opt/nix-fork")


def store_item(path: Path) -> Path | None:
    try:
        parts = path.resolve().parts
    except OSError:
        return None
    if len(parts) >= 4 and parts[1] == "nix" and parts[2] == "store":
        return Path(*parts[:4])
    return None


def elf_info(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return None
    e_phoff, = struct.unpack_from("<Q", data, 32)
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 54)
    loads = []
    interp = None
    dyn_off = dyn_size = None
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from("<IIQQQQQQ", data, off)
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 3:
            interp = data[p_offset:p_offset + p_filesz].split(b"\0", 1)[0].decode()
        elif p_type == 2:
            dyn_off, dyn_size = p_offset, p_filesz

    def vaddr_off(vaddr: int):
        for virt, file_off, size in loads:
            if virt <= vaddr < virt + size:
                return file_off + (vaddr - virt)
        return None

    needed = []
    rpaths = []
    if dyn_off is not None:
        tags = []
        for off in range(dyn_off, dyn_off + dyn_size, 16):
            tag, val = struct.unpack_from("<qQ", data, off)
            if tag == 0:
                break
            tags.append((tag, val))
        strtab = next((val for tag, val in tags if tag == 5), None)
        str_off = vaddr_off(strtab) if strtab is not None else None

        def cstr(rel: int) -> str:
            start = str_off + rel
            end = data.index(b"\0", start)
            return data[start:end].decode()

        if str_off is not None:
            for tag, val in tags:
                if tag == 1:
                    needed.append(cstr(val))
                elif tag in (15, 29):
                    rpaths.append(cstr(val))
    return interp, needed, rpaths


def expand_rpath(entry: str, binary: Path) -> Path:
    origin = str(binary.parent)
    entry = entry.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
    return Path(entry)


items: set[Path] = set()
seen_bins: set[Path] = set()


def add_item(item: Path) -> None:
    if item in items or not item.is_dir():
        return
    items.add(item)
    for root, dirnames, filenames in os.walk(item):
        for name in dirnames + filenames:
            candidate = Path(root) / name
            if not candidate.is_symlink():
                continue
            other = store_item(Path(os.path.realpath(candidate)))
            if other is not None and other != item:
                add_item(other)


def resolve_needed(name: str, search: list[Path]) -> Path | None:
    for directory in search:
        candidate = directory / name
        if candidate.exists():
            return candidate
    for directory in (PREFIX / "lib", PREFIX / "lib" / "x86_64-linux-gnu"):
        candidate = directory / name
        if candidate.exists():
            return candidate
    matches = glob.glob(f"/nix/store/*/lib/{name}") + glob.glob(f"/nix/store/*/lib64/{name}")
    return Path(matches[0]) if matches else None


def walk(path: Path) -> None:
    path = path.resolve()
    if path in seen_bins or not path.is_file():
        return
    info = elf_info(path)
    if info is None:
        return
    seen_bins.add(path)
    interp, needed, rpaths = info
    if interp:
        interp_path = Path(interp)
        item = store_item(interp_path)
        if item is not None:
            add_item(item)
    search = []
    for entry in rpaths:
        for piece in entry.split(":"):
            if not piece:
                continue
            directory = expand_rpath(piece, path)
            search.append(directory)
            item = store_item(directory)
            if item is not None:
                add_item(item)
    for name in needed:
        found = resolve_needed(name, search)
        if found is None:
            print("unresolved", name, "from", path)
            continue
        item = store_item(found)
        if item is not None:
            add_item(item)
            walk(found)


roots = []
for base in (PREFIX / "bin", PREFIX / "lib"):
    if not base.exists():
        continue
    for candidate in base.rglob("*"):
        name = candidate.name
        if candidate.is_file() and (name == "nix" or name.startswith("nix-") or name.startswith("libnix")):
            roots.append(candidate)

for root in roots:
    walk(root)

if not items:
    raise SystemExit("no /nix/store items found for the fork binary")

MIRROR.mkdir(parents=True, exist_ok=True)
for item in sorted(items):
    target = MIRROR / item.name
    if target.exists():
        continue
    print("mirror", item.name)
    shutil.copytree(item, target, symlinks=True)

print(f"mirrored {len(items)} store paths to {MIRROR}")
