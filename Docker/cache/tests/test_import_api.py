#!/usr/bin/env python3
# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

"""Offline test of the cache import API: certificates are checked on import.

No Docker and no Nix are needed. `nix-store` is replaced by a small stand-in
that restores a NAR as a plain file and records the path in a SQLite file
(simplified schema, enough for the import server). Certificates come from the
simulated CACTEE (Docker/cactee/cactee-server.py), signed with a throw-away key.
The last cases drive the simulator's own POST /certify, with a stand-in for
CACTEE's test_docker.py.

  python3 Docker/cache/tests/test_import_api.py        # one line per case
  python3 Docker/cache/tests/test_import_api.py -v     # + the server's own log

Needs python3, the `openssl` command and the `cryptography` package (the
simulator uses it). Exit status 0 when every case passes.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

VERBOSE = "-v" in sys.argv[1:]
HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE.parent
CACTEE_DIR = CACHE_DIR.parent / "cactee"
MANIFEST = CACHE_DIR / "cactee-manifest.json"

FAKE_NIX_STORE = '''#!/usr/bin/env python3
import sqlite3, sys
a = sys.argv[1:]
if a[0] == "--restore":
    open(a[1], "wb").write(sys.stdin.buffer.read())
    sys.exit(0)
if "--load-db" in a:
    root = a[a.index("--store") + 1].split("root=")[1]
    lines = sys.stdin.read().split("\\n")
    path, narhash, size, _deriver, nrefs = lines[:5]
    refs = lines[5:5 + int(nrefs)]
    con = sqlite3.connect(root + "/nix/var/nix/db/db.sqlite")
    valid = {r[0] for r in con.execute("select path from ValidPaths")} | {path}
    for r in refs:
        if r not in valid:
            print("error: path '%s' is not valid" % r, file=sys.stderr)
            sys.exit(1)
    con.execute("insert into ValidPaths(path,hash,narSize) values(?,?,?)",
                (path, "sha256:" + narhash, int(size)))
    for r in refs:
        con.execute("insert into Refs values(?,?)", (path, r))
    con.commit()
    sys.exit(0)
sys.exit(2)
'''

# Stand-in for CACTEE's test_docker.py: records what is in the inputs folder when
# it starts, then "builds" one NAR.
FAKE_TEST_DOCKER = '''
import json, sys
from pathlib import Path
cfg = json.loads(Path(sys.argv[1]).read_text())
tmp = Path("test_temp/tmp")
(tmp / "seen.txt").write_text(" ".join(sorted(p.name for p in (tmp / "inputs").iterdir())))
(tmp / "outputs" / "0123456789abcdfghijklmnpqrsvwxyz-x.nar").write_bytes(b"nar of " + cfg["name"].encode())
'''

work = Path(tempfile.mkdtemp(prefix="nixtee-test-"))
real_stdout = sys.stdout
if not VERBOSE:
    sys.stdout = open(os.devnull, "w")


def say(*parts: object) -> None:
    print(*parts, file=real_stdout, flush=True)


# --------------------------------------------------------------------------
# environment for the two servers (read when they are imported)
# --------------------------------------------------------------------------
bindir, root, att, tmp = (work / n for n in ("bin", "root", "att", "tmp"))
for d in (bindir, tmp, root / "nix/var/nix/db"):
    d.mkdir(parents=True)
stub = bindir / "nix-store"
stub.write_text(FAKE_NIX_STORE)
stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
con = sqlite3.connect(root / "nix/var/nix/db/db.sqlite")
con.execute("CREATE TABLE ValidPaths(id integer primary key, path text unique, hash text, narSize int)")
con.execute("CREATE TABLE Refs(referrer text, reference text)")
con.commit()
con.close()

MAN = json.loads(MANIFEST.read_text())
INFO = MAN["EXPECTED_DUET_SERVICE_INFO"]
os.environ.update(
    PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
    CACHE_ROOT=str(root), ATTESTATION_DIR=str(att), PUBLISH_TOKEN="t",
    MIN_FREE_BYTES="0", REQUIRE_CERTIFICATE="1", CACTEE_MANIFEST=str(MANIFEST),
    CACTEE_AZURE_SIMULATE="1",
    # the simulated CACTEE, configured as in docker-compose.yml
    CACTEE_MRENCLAVE=MAN["EXPECTED_DUET_ADMIN_MRENCLAVE"],
    CACTEE_SERVICE_CODE_HASH=INFO["service_code_hash"],
    CACTEE_SERVICE_DEPENDENCIES_HASH=INFO["service_dependencies_hash"]["cpu"],
    CACTEE_SERVICE_TOOLS_HASHES=json.dumps(INFO["service_tools_hashes"]),
)
sys.path.insert(0, str(CACHE_DIR))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import cactee_verify as cv  # noqa: E402

server = load("nar_import_server", CACHE_DIR / "nar-import-server.py")
cactee = load("cactee_server", CACTEE_DIR / "cactee-server.py")

server.REAL_STORE_DIR.mkdir(parents=True, exist_ok=True)
httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no proxy for localhost


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def put(path: str, data: bytes, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, data=data, method="PUT",
                                 headers={"Authorization": "Bearer t", **(headers or {})})
    try:
        with OPENER.open(req) as r:
            return r.status, r.read().decode().strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode().strip()


def get(path: str) -> str:
    with OPENER.open(BASE + path) as r:
        return r.read().decode()


def cert_for(store_path: str, content: bytes, mutate=None) -> bytes:
    """A certificate from the simulated CACTEE for `content` (the NAR)."""
    nar = tmp / (Path(store_path).name + ".nar")
    nar.write_bytes(content)
    cert = cactee._fake_certificate(nar, {"name": "x", "inputs": []})
    if mutate:
        mutate(cert)
    return json.dumps(cert).encode()


def hash_part(store_path: str) -> str:
    return Path(store_path).name.split("-", 1)[0]


results: list[bool] = []


def check(label: str, got: tuple[int, str], want_code: int, contains: str = "") -> None:
    ok = got[0] == want_code and contains in got[1]
    results.append(ok)
    say(f"{'PASS' if ok else 'FAIL'}  {label}\n        -> {got[0]} {got[1][:130]}")


def verdict(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    say(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        -> {detail[:130]}" if detail else ""))


def sign(cert: dict, key=None) -> None:
    cert["duet_admin_signature"] = (
        base64.b64encode(key.sign(*_pss(cert))).decode() if key
        else cactee._sign_service_result(cert["duet_service_result"])
    )


def _pss(cert: dict):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return (json.dumps(cert["duet_service_result"], sort_keys=True).encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256())


# --------------------------------------------------------------------------
# the cases
# --------------------------------------------------------------------------
A = "/nix/store/ynwgi3zd53dpkmycksvqmsh2z02d447f-bootstrap-stage0-stdenv-linux"
B = "/nix/store/pb9q1wnzlzzvlcy5g2p10v66czpmz72f-gnu-config-2024-01-01"
C = "/nix/store/9vqj41i15ssd2zs9l3ajd7i5qc5v5dbv-c"
D = "/nix/store/abijl898gbkzqwxyc73z1pmnpl3kjx5z-d"
E = "/nix/store/" + "1" * 32 + "-with-placeholder"
F = "/nix/store/" + "2" * 32 + "-with-unknown"
nar_a = b"NAR leaf content"
nar_b = ("binary uses " + A + "/lib").encode()

say("== certificate first, then NAR ==")
check("NAR without any certificate is refused (412)",
      put("/import", nar_a, {"X-Nix-Store-Path": A}), 412)
check("valid certificate is verified and kept as pending",
      put(f"/attest/{hash_part(A)}.json", cert_for(A, nar_a)), 200, "pending")
check("NAR matching the certificate is registered",
      put("/import", nar_a, {"X-Nix-Store-Path": A}), 200, A)
final = json.loads((att / f"{hash_part(A)}.json").read_text())
want = "sha256-" + base64.b64encode(hashlib.sha256(nar_a).digest()).decode()
verdict("certificate moved next to the store, outputHash = NAR hash computed by the cache",
        final["outputHash"] == want and not (att / "pending" / f"{hash_part(A)}.json").exists(),
        final["verified"])
check("re-import of identical content is accepted",
      put("/import", nar_a, {"X-Nix-Store-Path": A}), 200)

say("\n== a certified NAR cannot be swapped ==")
check("certificate for B verifies",
      put(f"/attest/{hash_part(B)}.json", cert_for(B, nar_b)), 200, "pending")
check("other bytes under the same certificate are refused (409, step 05)",
      put("/import", nar_b + b"evil", {"X-Nix-Store-Path": B}), 409, "does not match")
check("the certified bytes are accepted",
      put("/import", nar_b, {"X-Nix-Store-Path": B}), 200, B)


def tamper_result(c):      # change the signed result after signing
    c["duet_service_result"]["service_result"]["timestamp"] += 1


def other_enclave(c):      # quote of another enclave (the quote itself is not signed)
    q = bytearray(base64.b64decode(c["duet_admin_quote"]["quote"]))
    q[112:144] = b"\x22" * 32
    c["duet_admin_quote"]["quote"] = base64.b64encode(bytes(q)).decode()


def swap_key(c):           # attacker's key: signature checks with it, the quote binds the real key
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    c["duet_admin_quote"]["public_key"] = k.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    sign(c, k)


def other_service(c):      # a different service, properly re-signed: only step 03 can catch it
    c["duet_service_result"]["service_info"]["service_code_hash"] = "00" * 32
    sign(c)


say("\n== forged certificates are refused (422) ==")
hc = hash_part(C)
check("result altered after signing -> step 02",
      put(f"/attest/{hc}.json", cert_for(C, b"c", tamper_result)), 422, "02_controller_signature")
check("quote from another enclave -> step 01 (MRENCLAVE)",
      put(f"/attest/{hc}.json", cert_for(C, b"c", other_enclave)), 422, "mrenclave")
check("attacker's own signing key -> step 01 (report_data)",
      put(f"/attest/{hc}.json", cert_for(C, b"c", swap_key)), 422, "report_data")
check("other service code, validly signed -> step 03",
      put(f"/attest/{hc}.json", cert_for(C, b"c", other_service)), 422, "service_code_hash")
check("certificate of another package",
      put(f"/attest/{hc}.json", cert_for(D, b"d")), 422, "expected 1")
check("nothing was kept: the NAR is still refused (412)",
      put("/import", b"c", {"X-Nix-Store-Path": C}), 412)

say("\n== a path that is already in the store ==")
check("second certificate, same bytes",
      put(f"/attest/{hash_part(A)}.json", cert_for(A, nar_a)), 200, "already certified")
check("second certificate, other bytes (409)",
      put(f"/attest/{hash_part(A)}.json", cert_for(A, b"x")), 409)
paths = get("/paths").split()
verdict("the store holds only certified paths", paths == sorted([A, B]), " ".join(paths))

say("\n== dependencies ==")
ne = b"compiler wrote /nix/store/" + b"a" * 32 + b"-glibc-2.39/lib here"
nf = b"mentions /nix/store/" + b"b" * 32 + b"-foo/bin"
put(f"/attest/{hash_part(E)}.json", cert_for(E, ne))
check("bootstrap placeholder hash (all 'a') is not an uncertified dependency",
      put("/import", ne, {"X-Nix-Store-Path": E}), 200, E)
put(f"/attest/{hash_part(F)}.json", cert_for(F, nf))
check("mention of a path that is not certified yet is refused (409)",
      put("/import", nf, {"X-Nix-Store-Path": F}), 409, "not in the certified store")

say("\n== Azure attestation token (real mode, local stand-in for Azure's keys) ==")
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

azure_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
nums = azure_key.public_key().public_numbers()
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": b64u(nums.n.to_bytes(256, "big")),
                  "e": b64u(nums.e.to_bytes(3, "big"))}]}
cert = json.loads(cert_for(A, nar_a))
quote = base64.b64decode(cert["duet_admin_quote"]["quote"])
pem = cert["duet_admin_quote"]["public_key"].encode()
ENDPOINT = "https://sharedneu.neu.attest.azure.net"


def token(claims: dict, key=azure_key) -> str:
    head = b64u(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
    body = b64u(json.dumps(claims).encode())
    return f"{head}.{body}.{b64u(key.sign(f'{head}.{body}'.encode(), padding.PKCS1v15(), hashes.SHA256()))}"


good = {"iss": ENDPOINT, "sgx-mrenclave": quote[112:144].hex(),
        "x-ms-sgx-report-data": quote[368:400].hex() + "00" * 32,
        "x-ms-sgx-collateral": {"quotehash": hashlib.sha256(quote).hexdigest()}}
other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
for label, tok, expect_ok in [
    ("token vouching for the quote is accepted", token(good), True),
    ("token signed by another key is refused", token(good, other_key), False),
    ("token for another enclave is refused", token({**good, "sgx-mrenclave": "33" * 32}), False),
    ("token for another quote is refused", token({**good, "x-ms-sgx-collateral": {"quotehash": "44" * 32}}), False),
    ("token from another issuer is refused", token({**good, "iss": "https://evil.attest.azure.net"}), False),
]:
    problems = cv.verify_jwt(tok, ENDPOINT, jwks, quote, pem, MAN["EXPECTED_DUET_ADMIN_MRENCLAVE"])
    verdict(label, (not problems) == expect_ok, "; ".join(problems) or "valid")

cert["duet_admin_quote"]["jwt"] = token(good)
report = cv.verify_certificate(cert, MAN, simulate_azure=False, jwks=jwks)
verdict("certificate with token verifies when Azure is not simulated", report.ok, report.summary())
del cert["duet_admin_quote"]["jwt"]
report = cv.verify_certificate(cert, MAN, simulate_azure=False, jwks=jwks)
verdict("certificate without token is refused when Azure is not simulated",
        not report.ok, report.failures())

# --------------------------------------------------------------------------
# the simulated CACTEE itself
# --------------------------------------------------------------------------
say("\n== simulated CACTEE: harness inputs folder ==")
harness = work / "harness"
H_IN, H_OUT = harness / "test_temp/tmp/inputs", harness / "test_temp/tmp/outputs"
cactee.HARNESS, cactee.INPUTS, cactee.OUTPUTS = harness, H_IN, H_OUT   # read when called
(H_IN / "sub").mkdir(parents=True)
(H_IN / "old.export").write_bytes(b"left over from the previous package")
(H_IN / "sub" / "x").write_bytes(b"x")
cactee._reset_inputs()
verdict("leftovers of the previous package are removed, the folder stays",
        H_IN.is_dir() and not any(H_IN.iterdir()))
shutil.rmtree(H_IN)
cactee._reset_inputs()
verdict("a missing folder is created", H_IN.is_dir())

if hasattr(tarfile, "data_filter"):    # the server unpacks uploads with tarfile filters
    H_IN.mkdir(parents=True, exist_ok=True)
    H_OUT.mkdir(parents=True, exist_ok=True)
    (harness / "test_docker.py").write_text(FAKE_TEST_DOCKER)
    (H_IN / "old.export").write_bytes(b"left over from the previous package")
    sim = ThreadingHTTPServer(("127.0.0.1", 0), cactee.Handler)
    threading.Thread(target=sim.serve_forever, daemon=True).start()
    upload = io.BytesIO()
    with tarfile.open(fileobj=upload, mode="w:gz") as tar:
        raw = json.dumps({"name": "x", "inputs": []}).encode()
        info = tarfile.TarInfo("config.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    request = urllib.request.Request(
        f"http://127.0.0.1:{sim.server_address[1]}/certify", data=upload.getvalue(), method="POST")
    try:
        with OPENER.open(request, timeout=60) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    sim.shutdown()
    seen_file = harness / "test_temp/tmp/seen.txt"
    seen = seen_file.read_text() if seen_file.exists() else "<test_docker.py did not run>"
    verdict("POST /certify: test_docker.py starts with an empty inputs folder",
            status == 200 and seen == "", f"status {status}, it saw: {seen!r}")
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as answer:
            returned = json.load(answer.extractfile("asset_certificate.json"))
        report = cv.verify_certificate(returned, MAN, simulate_azure=True)
        verdict("POST /certify: the certificate it returns passes the cache's checks",
                report.ok, report.summary())
    except (tarfile.TarError, KeyError, AttributeError, ValueError) as exc:
        verdict("POST /certify: the certificate it returns passes the cache's checks", False, repr(exc))
else:
    say("SKIP  POST /certify cases: this Python has no tarfile filters (the container has them)")

httpd.shutdown()
shutil.rmtree(work, ignore_errors=True)
sys.stdout = real_stdout
say(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
