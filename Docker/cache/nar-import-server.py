#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""HTTP import API next to Harmonia.

Harmonia serves a chroot store at CACHE_ROOT (logical /nix/store). This
process accepts `nix-store --dump` NARs on port 5001 and registers them
there UNDER THEIR ORIGINAL STORE PATH, with their references.

  PUT /attest/<hash>.json          (first)
  Authorization: Bearer <PUBLISH_TOKEN>
  body: CACTEE asset certificate (JSON)

  PUT /import                      (then)
  Authorization: Bearer <PUBLISH_TOKEN>
  X-Nix-Store-Path: /nix/store/<hash>-<name>
  body: NAR bytes

  GET /paths    every valid path in the certified store
  GET /health

Certificates (REQUIRE_CERTIFICATE=1, the default)
-------------------------------------------------
Nothing enters the certified store without a certificate that verifies.

  /attest   checks what needs only the certificate (cactee_verify.py):
            01 SGX quote: MRENCLAVE = manifest, report_data = sha256(PEM),
               Azure attestation token (or CACTEE_AZURE_SIMULATE=1)
            02 controller RSA-PSS signature over the service result
            03 service_info = the expected service (CACTEE_MANIFEST)
            then keeps it as pending/<hash>.json.
  /import   receives the NAR, computes its sha256, and checks it against
            the certificate's output `<hash>-<name>.nar` (step 05) BEFORE
            restoring or registering anything. On success the certificate
            moves to <hash>.json, where Harmonia reads it.

So the NAR is downloaded first and verified against the certificate before
it is used, as the client does.

Why not `nix-store --add`
-------------------------
`--add` treats the unpacked NAR as a new source directory: it gets a new,
content-addressed hash and no references. The client asks for the original
(input-addressed) path and gets a 404. See orchestration-state.json.bak:
ynwgi3…-bootstrap-stage0-stdenv-linux was published as 4hs0jd….

What this server does instead
-----------------------------
Everything `nix-store --load-db` needs is derived here, from the NAR itself,
without trusting any side file from the publisher or from CACTEE:

  path        X-Nix-Store-Path header
  narHash     sha256 of the received NAR
  narSize     length of the received NAR
  references  hash parts found in the NAR that are valid in the certified
              store, plus the path's own hash (self-reference). This is how
              Nix itself computes references after a build: scan the output
              for the hashes of candidate paths.
  deriver     left empty (optional in Nix)

Then: nix-store --restore at the exact path, canonicalise, --load-db.

A NAR that mentions /nix/store/<hash> for a hash that is NOT in the
certified store is refused (409) by default: its dependency has not been
certified yet, and registering it would publish an incomplete closure.
Set UNKNOWN_REFS=warn to register anyway (the mention is then ignored).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cactee_verify

BIND = os.environ.get("PUBLISH_BIND", "0.0.0.0:5001")
TOKEN = os.environ.get("PUBLISH_TOKEN", "")
ATTESTATION_DIR = Path(
    os.environ.get("ATTESTATION_DIR", "/var/lib/nix-certification")
)
CACHE_ROOT = Path(os.environ.get("CACHE_ROOT", "/certified_store"))
NIX_STORE = f"local?root={CACHE_ROOT}"
REAL_STORE_DIR = CACHE_ROOT / "nix" / "store"
DB_PATH = CACHE_ROOT / "nix" / "var" / "nix" / "db" / "db.sqlite"
MAX_NAR_BYTES = int(os.environ.get("MAX_NAR_BYTES", str(8 * 1024 * 1024 * 1024)))
MIN_FREE_BYTES = int(os.environ.get("MIN_FREE_BYTES", str(2 * 1024 * 1024 * 1024)))
UNKNOWN_REFS = os.environ.get("UNKNOWN_REFS", "refuse").strip().lower()
REQUIRE_CERTIFICATE = os.environ.get("REQUIRE_CERTIFICATE", "1") == "1"
CACTEE_MANIFEST = os.environ.get("CACTEE_MANIFEST", "/etc/nixtee/cactee-manifest.json")
AZURE_SIMULATE = os.environ.get("CACTEE_AZURE_SIMULATE", "0") == "1"
PENDING_DIR = ATTESTATION_DIR / "pending"
MAX_CERT_BYTES = 512 * 1024

# Nix base32 alphabet: no e, o, t, u.
NIX32 = "0123456789abcdfghijklmnpqrsvwxyz"
# Bootstrap GCC writes /nix/store/aaa... into the compiler before the real
# libc path exists. It is a placeholder, not a package to certify first.
PLACEHOLDER_HASHES = {"a" * 32}
HASH_OK = re.compile(r"^[0-9a-df-np-sv-z]{32}$")
PATH_OK = re.compile(r"^/nix/store/([0-9a-df-np-sv-z]{32})-([A-Za-z0-9._+?=-]+)$")
# A run of base32 characters; hashes are searched inside runs.
RUN_RE = re.compile(rb"[0-9a-df-np-sv-z]{32,}")
# Explicit store-path mentions, used for the "unknown dependency" check.
MENTION_RE = re.compile(rb"/nix/store/([0-9a-df-np-sv-z]{32})-")
CHUNK = 4 * 1024 * 1024

# One import at a time: restore + register must not race on the same path,
# and the valid-path set read for the scan must not change underneath.
IMPORT_LOCK = threading.Lock()


class Refused(Exception):
    """Client-side error: maps to an HTTP status."""

    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------
# certified store (read side)
# --------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)


def valid_paths() -> dict[str, str]:
    """hashPart -> full store path, for every valid path in the certified store."""
    with _db() as con:
        rows = con.execute("SELECT path FROM ValidPaths").fetchall()
    out: dict[str, str] = {}
    for (path,) in rows:
        m = PATH_OK.match(path)
        if m:
            out[m.group(1)] = path
    return out


def path_nar_hash(store_path: str) -> str | None:
    """narHash as stored in the DB (e.g. 'sha256:<base16>'), or None."""
    with _db() as con:
        row = con.execute(
            "SELECT hash FROM ValidPaths WHERE path = ?", (store_path,)
        ).fetchone()
    return row[0] if row else None


def path_by_hash(hid: str) -> str | None:
    with _db() as con:
        row = con.execute(
            "SELECT path FROM ValidPaths WHERE path LIKE ?",
            (f"/nix/store/{hid}-%",),
        ).fetchone()
    return row[0] if row else None


def digest_from_db_hash(value: str) -> bytes | None:
    """'sha256:<base16|nix32|base64>' or SRI 'sha256-<base64>' -> 32 raw bytes."""
    if value.startswith("sha256-"):
        try:
            return base64.b64decode(value[7:])
        except binascii.Error:
            return None
    if value.startswith("sha256:"):
        value = value[7:]
    if len(value) == 64:
        try:
            return bytes.fromhex(value)
        except ValueError:
            return None
    if len(value) == 52:
        return nix32_decode(value)
    return None


def nix32_decode(s: str) -> bytes | None:
    """Nix's base32 (least-significant digit last, custom alphabet)."""
    n = 32
    out = bytearray(n)
    for i, ch in enumerate(reversed(s)):
        d = NIX32.find(ch)
        if d < 0:
            return None
        b = i * 5
        j, k = b // 8, b % 8
        out[j] |= (d << k) & 0xFF
        if j + 1 < n:
            out[j + 1] |= d >> (8 - k)
    return bytes(out)


# --------------------------------------------------------------------------
# NAR scan
# --------------------------------------------------------------------------

def scan_nar(nar: Path, candidates: set[bytes]) -> tuple[str, int, set[str], set[str]]:
    """One streaming pass over the NAR.

    Returns (sha256 hex, size, found candidate hashes, mentioned hashes).
    `found`: every 32-char window inside a base32 run that is a candidate
    (same rule as Nix's reference scanner). `mentioned`: hashes appearing
    as /nix/store/<hash>- , whether or not they are candidates.
    """
    h = hashlib.sha256()
    size = 0
    found: set[str] = set()
    mentioned: set[str] = set()
    carry = b""
    with nar.open("rb") as f:
        while True:
            chunk = f.read(CHUNK)
            eof = not chunk
            h.update(chunk)
            size += len(chunk)
            buf = carry + chunk
            cut = len(buf) if eof else _safe_cut(buf)
            # Runs never cross a non-base32 byte, and buf[cut] is one, so
            # every run inside buf[:cut] is complete.
            for m in RUN_RE.finditer(buf, 0, cut):
                run = m.group(0)
                for i in range(len(run) - 31):
                    w = run[i:i + 32]
                    if w in candidates:
                        found.add(w.decode())
            # A mention is 44 bytes; _safe_cut leaves >= MENTION_LEN bytes
            # after cut, so any mention starting before cut is whole in buf.
            for m in MENTION_RE.finditer(buf):
                if m.start() >= cut:
                    break
                mentioned.add(m.group(1).decode())
            carry = buf[cut:]
            if eof:
                break
    return h.hexdigest(), size, found, mentioned


MENTION_LEN = len(b"/nix/store/") + 32 + 1
NIX32_BYTES = frozenset(NIX32.encode())
MAX_CARRY = 64 * 1024 * 1024


def _safe_cut(buf: bytes) -> int:
    """Index of the last non-base32 byte at least MENTION_LEN from the end."""
    i = len(buf) - MENTION_LEN - 1
    while i >= 0 and buf[i] in NIX32_BYTES:
        i -= 1
    if i >= 0:
        return i
    # Whole buffer is one base32 run: keep carrying it, unless absurdly long
    # (then accept that a window across this cut may be missed).
    return 0 if len(buf) < MAX_CARRY else len(buf) - MENTION_LEN - 1


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def _canonicalise(root: Path) -> None:
    """Store-like metadata: read-only, mtime 1 (as Nix does). Keeps +x."""
    paths = [root]
    if root.is_dir() and not root.is_symlink():
        paths += list(root.rglob("*"))
    for p in reversed(paths):  # children before parents
        if p.is_symlink():
            try:
                os.utime(p, (1, 1), follow_symlinks=False)
            except (NotImplementedError, OSError):
                pass
            continue
        mode = p.stat().st_mode
        exe = mode & 0o111
        if p.is_dir():
            os.chmod(p, 0o555)
        else:
            os.chmod(p, 0o555 if exe else 0o444)
        os.utime(p, (1, 1))


def _remove(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        for p in [path, *path.rglob("*")]:
            if not p.is_symlink():
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# certificates
# --------------------------------------------------------------------------

_MANIFEST: dict | None = None


def manifest() -> dict:
    global _MANIFEST
    if _MANIFEST is None:
        try:
            _MANIFEST = cactee_verify.load_manifest(CACTEE_MANIFEST)
        except (OSError, ValueError, cactee_verify.CertificateError) as exc:
            raise Refused(500, f"cannot read CACTEE manifest {CACTEE_MANIFEST}: {exc}")
    return _MANIFEST


def certified_name(cert: dict, hid: str) -> str:
    """The certificate output '<hid>-<name>.nar' this certificate is for."""
    try:
        outputs = cert["duet_service_result"]["service_result"]["hashes"]["outputs"]
    except (KeyError, TypeError):
        outputs = None
    names = [k for k in (outputs or {}) if k.startswith(hid + "-") and k.endswith(".nar")]
    if len(names) != 1:
        raise Refused(422, f"certificate names {len(names)} output(s) <{hid}-…>.nar, expected 1")
    return names[0]


def verify_cert(cert: dict) -> "cactee_verify.Report":
    report = cactee_verify.verify_certificate(cert, manifest(), simulate_azure=AZURE_SIMULATE)
    if not report.ok:
        raise Refused(422, "certificate rejected: " + report.failures())
    return report


def final_cert(cert: dict, nar_sha_hex: str, report: str) -> dict:
    """Certificate as stored for Harmonia: the CACTEE certificate untouched,
    plus the summary fields Harmonia turns into Cvm* narinfo lines. The
    output hash is the one this server computed from the NAR."""
    out = dict(cert)
    out["type"] = str(cert.get("type") or "cactee-asset-certificate")
    out["backend"] = str(cert.get("backend") or "cactee")
    out["quote"] = str((cert.get("duet_admin_quote") or {}).get("quote", ""))
    out["outputHash"] = "sha256-" + base64.b64encode(bytes.fromhex(nar_sha_hex)).decode()
    out["verified"] = report
    return out


def _write_json(dest: Path, data: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(dest)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def accept_certificate(hid: str, cert: dict) -> str:
    """PUT /attest: verify steps 01-03, then keep it until the NAR arrives
    (or bind it now if the path is already in the store)."""
    nar_name = certified_name(cert, hid)
    report = verify_cert(cert)
    final = ATTESTATION_DIR / f"{hid}.json"
    with IMPORT_LOCK:
        store_path = path_by_hash(hid)
        if store_path is None:
            _write_json(PENDING_DIR / f"{hid}.json", cert)
            print(f"nar-import: certificate {hid} pending ({report.summary()})", flush=True)
            return f"pending {nar_name}: {report.summary()}"
        # Path already registered (shared source, re-publish): bind now.
        have = digest_from_db_hash(path_nar_hash(store_path) or "")
        step5 = cactee_verify.check_output(cert, nar_name, have.hex() if have else "")
        if not step5.ok:
            raise Refused(409, "certificate rejected: " + "; ".join(step5.detail))
        if final.exists():
            return f"already certified {store_path}"
        _write_json(final, final_cert(cert, have.hex(), report.summary() + " 05_output_hashes=ok"))
        return str(final)


def take_certificate(own_hash: str, nar_name: str, sha_hex: str) -> dict | None:
    """All checks for an incoming NAR, before it is written anywhere.
    Returns the certificate to store next to it, or None when certificates
    are not required and none was sent. Raises Refused otherwise."""
    pending = _read_json(PENDING_DIR / f"{own_hash}.json")
    if pending is None:
        if REQUIRE_CERTIFICATE:
            raise Refused(412, f"no verified certificate for {own_hash}: "
                               f"PUT /attest/{own_hash}.json before the NAR")
        return None
    # 01-03 again: the manifest may have changed since /attest.
    report = verify_cert(pending)
    step5 = cactee_verify.check_output(pending, nar_name, sha_hex)
    if not step5.ok:
        raise Refused(409, "NAR does not match its certificate: " + "; ".join(step5.detail))
    return final_cert(pending, sha_hex, report.summary() + " 05_output_hashes=ok")


def import_nar(nar: Path, store_path: str) -> str:
    """Register `nar` in the certified store as exactly `store_path`."""
    m = PATH_OK.match(store_path)
    if not m:
        raise Refused(400, f"bad store path {store_path!r}")
    own_hash, name = m.group(1), m.group(2)

    with IMPORT_LOCK:
        valid = valid_paths()
        candidates = {k.encode() for k in valid}
        candidates.add(own_hash.encode())

        sha_hex, size, found, mentioned = scan_nar(nar, candidates)

        # Idempotent re-import: same path, same content -> OK; else conflict.
        if own_hash in valid:
            existing = path_nar_hash(valid[own_hash])
            got = digest_from_db_hash(existing or "")
            if got is not None and got.hex() == sha_hex:
                print(f"nar-import: {store_path} already present", flush=True)
                return store_path
            raise Refused(
                409,
                f"{store_path} already registered with a different narHash "
                f"({existing}); received sha256:{sha_hex}",
            )

        # Step 05, before anything is written to the store.
        cert = take_certificate(own_hash, f"{own_hash}-{name}.nar", sha_hex)

        unknown = sorted(mentioned - set(valid) - {own_hash} - PLACEHOLDER_HASHES)
        if unknown and UNKNOWN_REFS != "warn":
            raise Refused(
                409,
                f"{store_path} mentions {len(unknown)} store path(s) that are "
                "not in the certified store (dependency not certified yet?): "
                + " ".join(unknown[:20]),
            )

        refs = sorted(valid[h] for h in found if h in valid)
        if own_hash in found:
            refs.append(store_path)
            refs.sort()

        dest = REAL_STORE_DIR / f"{own_hash}-{name}"
        if os.path.lexists(dest):
            # Leftover from a failed import (not registered): start clean.
            _remove(dest)

        try:
            with nar.open("rb") as src:
                subprocess.run(
                    ["nix-store", "--restore", str(dest)],
                    stdin=src, check=True, capture_output=True, text=True,
                )
            _canonicalise(dest)

            # Same text format as `nix-store --dump-db` (hash given).
            reginfo = "\n".join(
                [store_path, sha_hex, str(size), "", str(len(refs)), *refs]
            ) + "\n"
            subprocess.run(
                ["nix-store", "--store", NIX_STORE, "--load-db"],
                input=reginfo, check=True, capture_output=True, text=True,
            )
        except BaseException:
            _remove(dest)
            raise

        if cert is not None:
            _write_json(ATTESTATION_DIR / f"{own_hash}.json", cert)
            (PENDING_DIR / f"{own_hash}.json").unlink(missing_ok=True)

        note = f" refs={len(refs)}" + (" certified" if cert is not None else " UNCERTIFIED")
        if unknown:
            note += f" ignored-unknown={len(unknown)}"
        print(f"nar-import: registered {store_path} sha256:{sha_hex} "
              f"size={size}{note}", flush=True)
        return store_path


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"nar-import: {self.address_string()} {fmt % args}")

    def _reply(self, code: int, msg: str) -> None:
        body = (msg + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            self._reply(403, "PUBLISH_TOKEN is not set on the cache")
            return False
        got = self.headers.get("Authorization", "")
        want = "Bearer " + TOKEN
        if not hmac.compare_digest(got, want):
            self._reply(401, "missing or wrong Authorization: Bearer …")
            return False
        return True

    def _length(self) -> int | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, "Content-Length required")
            return None
        if length <= 0:
            self._reply(400, "empty body")
            return None
        return length

    def _drain(self, length: int) -> None:
        """Read and discard the body (avoids client Broken pipe on errors)."""
        left = length
        while left > 0:
            chunk = self.rfile.read(min(CHUNK, left))
            if not chunk:
                break
            left -= len(chunk)

    def _handle_attest(self, path: str) -> None:
        if not self._authorized():
            return
        name = path.rsplit("/", 1)[-1]
        hid = name[:-5] if name.endswith(".json") else ""
        if not HASH_OK.match(hid):
            self._reply(400, "path must be /attest/<32 nix32 chars>.json")
            return
        length = self._length()
        if length is None:
            return
        if length > MAX_CERT_BYTES:
            self._drain(length)
            self._reply(413, f"certificate larger than {MAX_CERT_BYTES} bytes")
            return
        raw = self.rfile.read(length)
        try:
            cert = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, "certificate must be JSON")
            return
        if not isinstance(cert, dict):
            self._reply(400, "certificate must be a JSON object")
            return
        try:
            self._reply(200, accept_certificate(hid, cert))
        except Refused as exc:
            print(f"nar-import: certificate {hid} refused: {exc}", flush=True)
            self._reply(exc.code, str(exc))

    def _handle_import(self) -> None:
        if not self._authorized():
            return
        store_path = (self.headers.get("X-Nix-Store-Path") or "").strip()
        length = self._length()
        if length is None:
            return
        if not PATH_OK.match(store_path):
            self._drain(length)
            self._reply(400, "X-Nix-Store-Path must be /nix/store/<32 nix32>-name")
            return
        if length > MAX_NAR_BYTES:
            self._drain(length)
            self._reply(413, f"NAR {length} bytes exceeds MAX_NAR_BYTES={MAX_NAR_BYTES}")
            return
        free = shutil.disk_usage(CACHE_ROOT).free
        # NAR on disk + its restored copy.
        if free < MIN_FREE_BYTES + 2 * length:
            self._drain(length)
            self._reply(
                507,
                f"not enough free space on {CACHE_ROOT} "
                f"(free={free}, need {MIN_FREE_BYTES}+2*{length})",
            )
            return
        fd, tmp_s = tempfile.mkstemp(prefix="nar-")
        os.close(fd)
        tmp = Path(tmp_s)
        try:
            left = length
            with tmp.open("wb") as out:
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
            if left:
                self._reply(400, f"body truncated ({left} bytes missing)")
                return
            self._reply(200, import_nar(tmp, store_path))
        except Refused as exc:
            self._reply(exc.code, str(exc))
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc))[-1500:]
            self._reply(500, f"nix-store failed: {err}")
        except Exception as exc:  # noqa: BLE001
            self._reply(500, str(exc))
        finally:
            tmp.unlink(missing_ok=True)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/"):
            self._reply(200, f"nar-import-ok max_nar_bytes={MAX_NAR_BYTES} "
                             f"unknown_refs={UNKNOWN_REFS} "
                             f"require_certificate={int(REQUIRE_CERTIFICATE)} "
                             f"azure_simulate={int(AZURE_SIMULATE)}")
            return
        if path == "/paths":
            try:
                paths = sorted(valid_paths().values())
            except sqlite3.Error as exc:
                self._reply(500, f"cannot read {DB_PATH}: {exc}")
                return
            self._reply(200, "\n".join(paths))
            return
        self._reply(404, "not found")

    def do_PUT(self):
        self.do_POST()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/attest/") and path.endswith(".json"):
            self._handle_attest(path)
            return
        if path == "/import":
            self._handle_import()
            return
        self._reply(404, "PUT /import or PUT /attest/<hash>.json")


def main() -> None:
    host, port_s = BIND.rsplit(":", 1)
    ATTESTATION_DIR.mkdir(parents=True, exist_ok=True)
    REAL_STORE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"nar-import listening on {BIND} (token {'set' if TOKEN else 'MISSING'}, "
          f"unknown_refs={UNKNOWN_REFS}, require_certificate={int(REQUIRE_CERTIFICATE)}, "
          f"manifest={CACTEE_MANIFEST}, azure_simulate={int(AZURE_SIMULATE)})")
    if REQUIRE_CERTIFICATE:
        manifest()  # fail at start, not at the first upload
    ThreadingHTTPServer((host, int(port_s)), Handler).serve_forever()


if __name__ == "__main__":
    main()